# Personal Dictation

Local-only macOS dictation. Hold Right Option (configurable in Settings), speak, release; text pastes into the focused app. Python + rumps + mlx-whisper (`small.en` INT4), py2app bundle in `dist/`, launchd auto-launch.

## How to Run

- **Installed:** launchd copy runs on login, restarts on crash. Manual: `open -a "Personal Dictation"`.
- **Debug from terminal:**
```bash
source .venv/bin/activate
DICTATION_DEBUG=1 python -m src
```
  `DICTATION_DEBUG=1` skips the single-instance fcntl lock and mirrors logs to the console. Without it, `python -m src` exits silently while the launchd copy holds the lock (logs only go to `~/.config/dictation/dictation.log`). launchd process has no TTY and env unset, so unaffected.
- **Tests:** `python tests/run_tests.py test_postprocessing` (no args = full suite; tests live in `tests/stress_test.py`).
- **Rebuild .app after dependency changes or a folder move:** `./rebuild.sh` (py2app -A + resets the TCC grants the rebuild invalidates + relaunches). Then re-grant Accessibility + Input Monitoring.
- **Auto-launch:** `./install.sh` / `./uninstall.sh`

## Permissions

Grant **Input Monitoring** (hotkey CGEvent tap), **Accessibility** (CGEvent Cmd+V paste; also the active tap a non-modifier hotkey needs) and **Microphone** to `Personal Dictation.app`. Debugging from Terminal: grant them to Terminal instead. After any rebuild these grants go stale; see Common Mistakes.

## Architecture

`src/` modules, one responsibility each. Non-obvious ones:
- `app.py` — rumps menubar app, state machine, orchestrator; `PROBLEMS` = ⚠ menu items for missing/stale grants (startup preflight + tap watchdog), self-clearing
- `hotkey.py` — configurable hold key. Modifiers: `flagsChanged` on a listen-only tap (Input Monitoring). F-keys/arrows: keyDown/keyUp swallowed on an active tap (Accessibility). Capture mode feeds the Settings recorder
- `settings.py` / `settings_window.py` — `settings.json` + the hotkey recorder window
- `audio.py` — sounddevice capture at native rate, downsample to 16kHz; streaming accessors `samples_captured`/`recent_peak`/`extract_16k`/`stop_stream`
- `segmenter.py` — streaming: closes speech segments at pauses during the hold, transcribes in background (poll thread + serial worker); speculative tail at short pauses
- `postprocess.py` — fuzzy vocab, filler removal, stutter collapse
- `paste.py` — clipboard save/restore + CGEvent Cmd+V (needs PostEvent, granted via Accessibility)
- `overlay.py` — floating waveform NSPanel: recording, processing-pulse, discard modes
- `config.py` — single source of truth: `CONFIG_DIR` + all behavioral tunables (paste delay, gain cap, thresholds, streaming)
- `launch.py` (root) — py2app entry point, sets sys.path, imports `src.app`

**Threads:** main (rumps NSApplication run loop + CGEvent tap callback, must stay <1ms) / audio (PortAudio) / segmenter poll + serial worker (streaming, during the hold) / processing (per-dictation daemon thread: finalize + paste) / warmup (one-shot model JIT at startup).

**CGEvent tap + rumps:** tap's CFMachPort is added to `CFRunLoopGetMain()` before `NSApplication.run()`; works because rumps drives the same main CFRunLoop.

## Common Mistakes

- **`beam_size` unsupported in mlx-whisper** — omit (greedy is default).
- **Don't revert `temperature` to mlx-whisper's default** — keep `(0.0, 0.2)` + pinned thresholds in `transcribe._DECODE_PARAMS` (see Decisions).
- **All AppKit ops from background threads go through `AppHelper.callAfter()`** — NSPanel, NSTimer, AND `self.title` (rumps → `NSStatusItem.setTitle_`). Direct calls from the processing thread crash. `_set_state` dispatches via `_apply_title`.
- **py2app alias mode needs rebuild after new dependencies** — symlinks source, not new packages.
- **py2app alias mode needs rebuild after moving the folder**: `__boot__.py` and `Info.plist` bake in the absolute source path, so the old bundle segfaults on launch (2026-09-24). Rebuild, then reset privacy grants (next entry).
- **Any rebuild invalidates privacy grants** — the ad-hoc signature's cdhash changes, but System Settings still shows the toggles ON. Symptoms: menubar ⚠ with "Hotkey not receiving keys" / "Paste blocked" items. Toggling isn't enough; `./rebuild.sh` resets all three (ListenEvent, PostEvent, Accessibility) and relaunches; re-grant Accessibility + Input Monitoring. Confirm with `log show --last 5m --predicate 'subsystem == "com.apple.TCC" AND eventMessage CONTAINS "Failed to match"'`.
- **PyObjC selector naming:** underscores map to multi-arg selectors; use camelCase for single-arg methods (`updateLevel_`, not `update_level_`).
- **Python floats through ObjC dispatch become NSNumber** — keep numeric math in pure Python, not ObjC-bridged methods.
- **NSPasteboard ops must handle None items/types.**
- **VAD runs before normalization** — Silero trained on real mic levels; post-gain noise triggers false positives.
- **rumps swallows exceptions in menu callbacks silently** — wrap handlers with the `_logged` decorator in `app.py`.
- **Track and invalidate every overlay NSTimer on each state change** — repeating animation timer + one-shot discard auto-hide. `_cancel_discard` is called in `show`/`show_processing`/`hide`/`flash_discard`; a stale discard timer otherwise hides the pill mid-recording.
- **Clipboard insertion is serialized** — `paste.insert_text` holds a module-level lock around save→set→paste→restore; concurrent inserts (re-paste click mid-paste) clobber the saved clipboard.
- **Every text `open()` and the log handler need `encoding="utf-8"`** — launchd can run with an ASCII locale; a default `open()` raised on "ń" in `history.append` after the paste had landed (false error + lost entry). For the log handler: default `RotatingFileHandler` silently DROPS any line with non-ASCII (encode raises in `emit`, logging swallows it). Only reproduces in .app/launchd, never in a terminal run. Verify deploys against `~/.config/dictation/dictation.log`.
- **Worktree `.venv`/`models` symlinks must stay untracked** — `.gitignore` uses slash-less `.venv`/`models` because `.venv/` matches a directory, not a symlink; `git add -A` staged the symlinks once and merging them replaced the real dirs with broken links (forced venv+model rebuild). In a worktree, add explicit paths, never `git add -A`.
- **Never recreate the event tap inside its own callback** — the Settings recorder receives keys from the tap, so applying a new hotkey is deferred with `AppHelper.callAfter`.
- **Deploy = quit the app, then start the job:** `pkill -f "Personal Dictation.app/Contents/MacOS"; launchctl kickstart -k gui/$(id -u)/com.personal.dictation`. `kickstart` alone only restarts the `open -W` wrapper, which re-attaches to the still-running old app (code loads at launch; confirm a new "starting" log line). A clean quit (status 0, incl. `test_app_lifecycle`'s pkill) is never relaunched by `KeepAlive`.
- **Streaming release must not join threads on the main thread** — `_on_release` is the tap callback (<1ms): it only records the release point + min-duration check and spawns `_finalize_streaming` (stop_polling + close_tail + clipboard save + finalize + paste) on a daemon thread. Inside it, `audio.stop_stream()` blocks ~110ms (PortAudio stop), so it runs on its own thread beside the decode, joined before IDLE.
- **The tail ends at the sample count read at key-up, before the stop sound** — read later, the chime lands in the tail and every speculative reuse misses.

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
- **Speculative tail** (`STREAM_SPECULATE`, 2026-10-09) — at a `STREAM_SPEC_SILENCE_S`=0.3s pause the idle worker pre-transcribes pending audio; a segment reuses it only if nothing after it exceeds the pause's own noise floor × `STREAM_SPEC_FLOOR_RATIO` (for `STREAM_SPEC_MIN_RUN` consecutive frames; lone frames are clicks), the absolute peak, or any Silero frame ≥0.5 (trust: a wrong reuse drops words, a miss only costs latency). Don't veto with `has_speech` — its 250ms minimum missed 5/7 quiet words. Only below `STREAM_MIN_SEG_S` pending. Real model, TTS: 0.8s pause before release → ~300ms→15ms, WER unchanged; ≤0.2s pause = no gain, no cost. Tune from the `spec=hit|miss(why)`/`pause=` log fields.
- **Idle penalty: re-warm on press + wired memory** — release p90 2.9s after >2h idle vs ~0.4s warm; the 10–60min bucket is fine, so the penalty grows with idle time (paging, not App Nap). `rewarm_if_idle` runs as the segmenter's `prime` after `REWARM_IDLE_S`; `MLX_WIRED_LIMIT_MB`=1024 keeps weights + the 30s-window working set (~940MB peak) resident. Verify with `Rewarm after` lines vs `release_to_paste` after long idle.
- **Dictation log timing** — `in Xms` = finalize wait only; `release_to_paste` (since 2026-10-09) = what the user feels. Older lines lacked it and hid a 0–350ms poll-thread wait.
- **Hotkey recorder allows modifiers + non-typing keys only** — letters/digits/punctuation would be swallowed system-wide; Escape is cancel; Caps Lock toggles. Modifiers capture on release (so fn+F5 records F5). Non-modifier keys need an active tap, which stalls all typing if the main thread blocks — another reason the tap callback stays <1ms.
- **Keep `small.en-q4`** (re-evaluated 2026-10-09; first 2026-06-16) — its fixed 30s-window encoder costs ~145ms/call, most of a short clip; models that encode only the real length win short clips. **Granite Speech 5.0 TurboCTC** is the only contender: 48ms vs 239ms short-clip median, similar WER, but no casing/punctuation (the `punctuators` restorer mangles jargon: "CodeX", "PostGres"), worse long-clip WER, needs Python ≥3.10 + mlx-audio/transformers, +0.5GB RAM. Adopt only if casing+punctuation keeps jargon right in <50ms AND an A/B on the owner's own recordings matches WER/jargon AND the owner accepts the deps. Rejected: Parakeet v2/v3 (~90ms faster, garbles "GitHub"/"Claude"), Apple SpeechTranscriber (~3x WER; contextualStrings had no effect), Moonshine v2 (2–3x WER), large-v3-turbo (most accurate, 4x slower), distil-medium (slower), base.en (5x errors). mlx 0.32 / Python 3.13 alone = no speedup. Truncating Whisper's 30s window is untested (likely hurts accuracy).

Shipped features: `FEATURES.md`. Session history: `logs/`.
