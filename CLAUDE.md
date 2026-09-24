# Personal Dictation

Local-only macOS dictation. Hold Right Option, speak, release; text pastes into the focused app. Python + rumps + mlx-whisper (`small.en` INT4), py2app bundle in `dist/`, launchd auto-launch.

## How to Run

- **Installed:** launchd copy runs on login, restarts on crash. Manual: `open -a "Personal Dictation"`.
- **Debug from terminal:**
```bash
source .venv/bin/activate
DICTATION_DEBUG=1 python -m src
```
  `DICTATION_DEBUG=1` skips the single-instance fcntl lock and mirrors logs to the console. Without it, `python -m src` exits silently while the launchd copy holds the lock (logs only go to `~/.config/dictation/dictation.log`). launchd process has no TTY and env unset, so unaffected.
- **Tests:** `python tests/run_tests.py test_postprocessing` (no args = full suite; tests live in `tests/stress_test.py`).
- **Rebuild .app after dependency changes:** `source .venv/bin/activate && python setup.py py2app -A`
- **Auto-launch:** `./install.sh` / `./uninstall.sh`

## Permissions

Grant **Accessibility** (CGEvent tap + AppleScript paste) and **Microphone** to `Personal Dictation.app`. Debugging from Terminal: grant them to Terminal instead.

## Architecture

`src/` modules, one responsibility each. Non-obvious ones:
- `app.py` — rumps menubar app, state machine, orchestrator
- `audio.py` — sounddevice capture at native rate, downsample to 16kHz; streaming accessors `samples_captured`/`recent_peak`/`extract_16k`/`stop_stream`
- `segmenter.py` — streaming: closes speech segments at pauses during the hold, transcribes in background (poll thread + serial worker)
- `postprocess.py` — fuzzy vocab, filler removal, stutter collapse
- `paste.py` — clipboard save/restore + AppleScript paste
- `overlay.py` — floating waveform NSPanel: recording, processing-pulse, discard modes
- `config.py` — single source of truth: `CONFIG_DIR` + all behavioral tunables (paste delay, gain cap, thresholds, streaming)
- `launch.py` (root) — py2app entry point, sets sys.path, imports `src.app`

**Threads:** main (rumps NSApplication run loop + CGEvent tap callback, must stay <1ms) / audio (PortAudio) / processing (per-dictation daemon thread: inference + paste) / warmup (one-shot model JIT at startup).

**CGEvent tap + rumps:** tap's CFMachPort is added to `CFRunLoopGetMain()` before `NSApplication.run()`; works because rumps drives the same main CFRunLoop.

## Common Mistakes

- **`beam_size` unsupported in mlx-whisper** — omit (greedy is default).
- **Don't revert `temperature` to mlx-whisper's default** — keep `(0.0, 0.2)` + pinned thresholds in `transcribe._DECODE_PARAMS` (see Decisions).
- **All AppKit ops from background threads go through `AppHelper.callAfter()`** — NSPanel, NSTimer, AND `self.title` (rumps → `NSStatusItem.setTitle_`). Direct calls from the processing thread crash. `_set_state` dispatches via `_apply_title`.
- **py2app alias mode needs rebuild after new dependencies** — symlinks source, not new packages.
- **PyObjC selector naming:** underscores map to multi-arg selectors; use camelCase for single-arg methods (`updateLevel_`, not `update_level_`).
- **Python floats through ObjC dispatch become NSNumber** — keep numeric math in pure Python, not ObjC-bridged methods.
- **NSPasteboard ops must handle None items/types.**
- **VAD runs before normalization** — Silero trained on real mic levels; post-gain noise triggers false positives.
- **rumps swallows exceptions in menu callbacks silently** — wrap handlers with the `_logged` decorator in `app.py`.
- **Track and invalidate every overlay NSTimer on each state change** — repeating animation timer + one-shot discard auto-hide. `_cancel_discard` is called in `show`/`show_processing`/`hide`/`flash_discard`; a stale discard timer otherwise hides the pill mid-recording.
- **Clipboard insertion is serialized** — `paste.insert_text` holds a module-level lock around save→set→paste→restore; concurrent inserts (re-paste click mid-paste) clobber the saved clipboard.
- **Log handler must be `encoding="utf-8"`** — launchd has no UTF-8 locale; default `RotatingFileHandler` silently DROPS any line with non-ASCII (encode raises in `emit`, logging swallows it). Only reproduces in .app/launchd, never in a terminal run. Verify deploys against `~/.config/dictation/dictation.log`.
- **Worktree `.venv`/`models` symlinks must stay untracked** — `.gitignore` uses slash-less `.venv`/`models` because `.venv/` matches a directory, not a symlink; `git add -A` staged the symlinks once and merging them replaced the real dirs with broken links (forced venv+model rebuild). In a worktree, add explicit paths, never `git add -A`.
- **Streaming release must not join threads on the main thread** — `_on_release` is the tap callback (<1ms). `segmenter.stop_polling()` can block one poll interval, so release only does the min-duration check and spawns `_finalize_streaming` (stop_polling + close_tail + finalize + paste) on a daemon thread.

## Decisions

- **Python-only** (no Swift split) — validate the core loop first.
- **Audio normalization before inference** — boosts quiet mics, gain capped at 100x.
- **Silero VAD as hallucination guard** — on raw audio before normalization; ~30ms on a ~12s clip, scales with length. Orange flash when VAD rejects.
- **2-step temperature `(0.0, 0.2)`** in `transcribe._DECODE_PARAMS` — mlx default is a 6-step fallback tuple that re-decodes a 30s window up to 6x on a threshold trip; log analysis showed ~7% of dictations hit it at 2–13x latency. `(0.0, 0.2)` caps worst case at 2 decodes; clean speech still passes first decode. Scalar `0.0` rejected: the INT4 model occasionally loops on greedy decode and would paste repeating garbage (trust-damaging). Thresholds pinned at mlx defaults (2.4 / -1.0 / 0.6) for version stability. Retry firing is logged; may drop to single greedy pass later with data.
- **150ms clipboard restore delay** — conservative for Electron; tunable.
- **py2app alias mode** — shell wrappers don't get macOS GUI sessions; alias symlinks source so edits apply on next launch.
- **launchd `KeepAlive` with `SuccessfulExit: false`** — restarts on crash, not intentional quit; 30s throttle against crash loops.
- **fcntl file lock** for single instance — prevents duplicate menubar icons.
- **Prefer built-in "MacBook" mic by name, fall back to default** — opening a Bluetooth mic forces AirPods from A2DP to HFP (phone-quality output).
- **Record at native rate, `np.interp` downsample to 16kHz** — avoids forcing PortAudio to reconfigure hardware.
- **Stop-not-close stream reuse** — `stop_recording()` keeps the stream for instant restart (kills prepare() race + 50–200ms recreate cost); orange mic dot still disappears. Always-on mic rejected for privacy.
- **Central config in `src/config.py`** — `CONFIG_DIR` was duplicated in 4 files (rename-drift trap). Promote cross-cutting tunables there; leave pure rendering geometry (overlay bar sizes) local.
- **Processing overlay persists from release until paste lands** — hidden on every `_process` exit (success/empty/exception) so it never sticks.
- **"Not ready" cue, not auto-queue** — hotkey press during WARMING_UP/PROCESSING plays a busy sound and is dropped. Auto-queue would clip the start of the next utterance in hold-to-talk.
- **Last-transcription menubar item = paste-failure recovery** — paste is a blind Cmd+V with no success check; the item re-pastes `_last_text`. Failed transcriptions keep audio for "Retry last dictation".
- **Configurable model size is a non-goal** — SPEC.md: fixed model, no adaptive selection. Owner confirmed 2026-06-16: no model picker.
- **Streaming transcription** (`segmenter.py`) — closes segments at pauses after `STREAM_MIN_SEG_S`=8s of unsegmented audio + quiet trailing window, or `STREAM_MAX_SEG_S`=24s hard cap (before Whisper's 30s window); one serial worker (one GPU). Release latency stops scaling with length (58s clip: 1623ms→327ms, WER delta 0.000). <8s dictations never segment = prior batch behavior. Each segment self-normalizes + runs VAD; `condition_on_previous_text=False` prevents cross-segment repetition. Retry reconstructs audio via `segmenter.full_audio()`.
- **Keep `small.en-q4`; no faster model wins on M3** (evaluated 2026-06-16) — distil-medium.en slower and less accurate (full medium encoder dominates short clips); base.en-q4 2.5x faster but ~5x word errors; Parakeet tdt-0.6b-v2 same accuracy, ~20% faster short clips but equal on 58s, plus new dependency. Decoder-pruning speedups are a large-model/server-GPU property. Long-dictation latency win came from streaming, not a model swap.

Shipped features: `FEATURES.md`. Session history: `logs/`.
