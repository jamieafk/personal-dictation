# Personal Dictation

Local-only macOS dictation tool. Hold Right Option, speak, release — text appears in the focused app.

## How to Run

**Primary (auto-launch):** Already running via launchd if installed. Starts on login, restarts on crash.

**Manual launch:** `open -a "Personal Dictation"` (uses the py2app .app bundle in `dist/`)

**Debug from terminal:**
```bash
cd /path/to/personal-dictation
source .venv/bin/activate
DICTATION_DEBUG=1 python -m src
```
`DICTATION_DEBUG=1` skips the single-instance lock (so a debug copy runs alongside the
installed launchd one) and mirrors logs to the console. Without it, `python -m src` exits
immediately whenever the app is installed (the launchd copy holds the lock) and the
terminal stays blank (logs only go to `~/.config/dictation/dictation.log`).

**Run a single test module:** `python tests/run_tests.py test_postprocessing` (no args = full suite).

**Rebuild .app after dependency changes:** `source .venv/bin/activate && python setup.py py2app -A`

**Install/uninstall auto-launch:** `./install.sh` / `./uninstall.sh`

## Permissions Required

- **Accessibility** — for CGEvent tap (hotkey detection) and AppleScript paste. Grant to `Personal Dictation.app`.
- **Microphone** — for audio capture via sounddevice. Grant to `Personal Dictation.app`.

When running from Terminal for debugging, grant permissions to Terminal instead.

## Architecture

Modules in `src/`, one responsibility each:

| Module | Responsibility |
|--------|---------------|
| `src/app.py` | Menubar app (rumps), state machine, orchestrator |
| `src/hotkey.py` | CGEvent tap for Right Option hold-to-talk |
| `src/audio.py` | sounddevice capture at native rate, downsample to 16kHz; streaming accessors (`samples_captured`/`recent_peak`/`extract_16k`/`stop_stream`) |
| `src/transcribe.py` | mlx-whisper inference, model lifecycle |
| `src/segmenter.py` | Streaming: closes speech segments at pauses during the hold and transcribes them in the background (poll thread + serial worker) |
| `src/postprocess.py` | Text cleanup: fuzzy vocab, filler removal, stutter collapse |
| `src/paste.py` | Clipboard save/restore + AppleScript paste |
| `src/sounds.py` | Audio feedback (Tink.aiff on press/release) |
| `src/history.py` | Transcription history persistence to disk |
| `src/history_window.py` | History browser (NSWindow with scrollable entries) |
| `src/overlay.py` | Floating neon waveform indicator (NSPanel) — recording + processing-pulse + discard modes |
| `src/config.py` | Single source of truth: `CONFIG_DIR` + all behavioral tunables (paste delay, gain cap, thresholds, streaming) |

Plus `launch.py` at the project root — py2app entry point that sets up sys.path and imports `src.app`.

### Threading Model

- **Main thread:** rumps NSApplication run loop + CGEvent tap callback (must stay under 1ms)
- **Audio thread:** managed by sounddevice/PortAudio
- **Processing thread:** per-dictation daemon thread for inference + paste
- **Warmup thread:** one-shot at startup for model JIT compilation

### Key Integration: CGEvent Tap + Rumps

The tap's CFMachPort is added to CFRunLoopGetMain() before rumps starts NSApplication.run(). This works because rumps drives the same main CFRunLoop.

## Common Mistakes

- `beam_size` is not supported in mlx-whisper — omit it (greedy decoding is the default)
- **Don't revert `temperature` to mlx-whisper's default** — its default is a 6-step fallback tuple that re-decodes a window up to 6x on a threshold trip (2–13x latency spikes). Keep the pinned short schedule `(0.0, 0.2)` + thresholds in `transcribe._DECODE_PARAMS` (full rationale under Decisions).
- NSPasteboard operations must handle None items/types gracefully
- **All UI operations from background threads must use `AppHelper.callAfter()`** — calling NSPanel/NSTimer directly from the processing thread crashes the app
- **py2app needs rebuild** (`python setup.py py2app -A`) after adding new dependencies — alias mode symlinks to source but not to new packages
- PyObjC method names with underscores map to multi-arg ObjC selectors — use camelCase for single-arg methods (e.g., `updateLevel_` not `update_level_`)
- Python floats through ObjC dispatch become NSNumber — keep numeric calculations in pure Python, not in ObjC-bridged methods
- **VAD must run before audio normalization** — Silero was trained on real mic levels. Running it after gain boost causes amplified noise to trigger false positives.
- **rumps silently swallows exceptions in menu callbacks** — errors in callback handlers (e.g., `_show_history`) produce no log output. Wrap with try/except + logging (the `_logged` decorator in `app.py`).
- **`self.title` is an AppKit op too** — setting the menubar title goes through rumps to `NSStatusItem.setTitle_`. From background threads (the `_process` finally, `_warmup`) it must be dispatched via `AppHelper.callAfter` like any other UI op, not just NSPanel/NSTimer. `_set_state` does this through `_apply_title`.
- **Coordinate multiple NSTimers across state transitions** — the overlay runs two timers (the repeating animation timer and the one-shot discard auto-hide). Each must be tracked and invalidated on every state change (`_cancel_discard` is called in `show`/`show_processing`/`hide`/`flash_discard`). A stale one-shot discard timer will otherwise fire later and hide the pill mid-recording.
- **Clipboard insertion must be serialized** — `paste.insert_text` holds a module-level lock around save→set→paste→restore. Concurrent inserts (e.g. a re-paste click during another paste) would otherwise clobber each other's saved clipboard value.
- **Log file must be UTF-8** — under launchd there is no UTF-8 locale, so a `RotatingFileHandler` with the default encoding silently DROPS any log line containing non-ASCII (em-dashes, glyphs): the encode raises in `emit`, logging swallows it, and the line never reaches the file (the next ASCII line writes fine, so it looks like a line vanished mid-sequence). Always pass `encoding="utf-8"`. This only reproduces in the .app/launchd context — a terminal run (UTF-8 locale) hides it. Verify deploys against `~/.config/dictation/dictation.log`, not just a terminal run.
- **Worktree shared-env symlinks must stay untracked** — sharing the real `.venv`/`models` into a git worktree via symlinks is fine, but a trailing-slash `.gitignore` pattern (`.venv/`) matches a *directory*, NOT a symlink of the same name, so `git add -A` will STAGE the symlinks. Committing + merging self-referential `.venv`/`models` symlinks replaced the real dirs with broken links and forced a venv+model rebuild. `.gitignore` now uses slash-less `.venv`/`models`. In a worktree, never `git add -A` blindly — add explicit paths.
- **Streaming release must not join threads on the main thread** — `_on_release` is the CGEvent tap callback (main run loop, <1ms budget). `segmenter.stop_polling()` joins the poll thread (can block up to one poll interval). So release does only the min-duration check on the main thread and spawns `_finalize_streaming` (stop_polling + close_tail + finalize + paste) on a daemon thread.

## Decisions

- **Python-only MVP** over Swift+Python split — validates core loop before adding complexity
- **Audio normalization** before inference — boosts quiet mics, capped at 100x gain
- **Silero VAD** for hallucination guard — runs on raw audio BEFORE normalization (~30ms on a ~12s clip; scales with clip length — the old "~3ms" figure was wrong). Replaced the crude 3-word minimum guard. Discard feedback (orange flash) shown when VAD rejects.
- **2-step temperature schedule `(0.0, 0.2)`** for Whisper decode (`transcribe._DECODE_PARAMS`) — mlx-whisper's default `temperature` is a 6-step fallback tuple (0.0→1.0) that silently re-decodes a 30s window up to 6x when a confidence/repetition threshold trips. Live-log analysis (2026-06-16) found ~7% of dictations hit this and ran 2–13x slower for the same word count (~90s cumulative waste). `(0.0, 0.2)` caps the worst case at 2 decodes — clean speech still passes at t=0.0 on the first decode (no latency change for normal dictation), and one cheap retry remains as the loop-escape hatch the INT4 model occasionally needs (greedy argmax on quantized logits can lock into a repetition loop). Hard-disabling fallback (scalar `0.0`) was rejected: its worst case is pasting repeating garbage at the cursor — a trust-damaging core-loop failure. Thresholds pinned at mlx defaults (2.4 / -1.0 / 0.6) so the retry only fires on genuine failures and behavior is version-stable. Telemetry logs when the retry fires, so we can drop to a single greedy pass later with data.
- **150ms clipboard restore delay** — conservative for Electron apps, tunable later
- **py2app alias mode** for .app bundle — shell script wrappers don't get macOS GUI sessions. Alias mode symlinks to source so edits take effect on next launch.
- **launchd Launch Agent** for auto-launch — `KeepAlive` with `SuccessfulExit: false` restarts on crash but not intentional quit. 30s throttle prevents rapid-crash loops.
- **fcntl file lock** for single-instance guard — prevents duplicate menubar icons
- **Built-in mic preference** over Bluetooth — opening a mic on AirPods/Bluetooth forces macOS to switch from A2DP (hi-fi) to HFP (phone quality), changing perceived volume on output. App finds "MacBook" mic by name and falls back to default only if none found.
- **Native sample rate recording** with downsample to 16kHz — avoids forcing PortAudio to reconfigure hardware sample rate. Records at device's default rate, then `np.interp` downsamples for Whisper.
- **Stop-not-close stream reuse** — `stop_recording()` stops the stream but keeps it alive for instant restart. Eliminates the prepare() race and ~50-200ms stream recreation cost. Orange mic dot disappears when stopped (verified). Always-on mic was rejected for privacy.
- **Central config over scattered literals** — `src/config.py` holds `CONFIG_DIR` (was duplicated in 4 files, a rename-drift trap) and all behavioral tunables. Promote cross-cutting tunables here; leave pure rendering geometry (overlay bar sizes) local.
- **Processing overlay, not hide-at-release** — the pill stays as a pulse from release until the paste lands, so there's feedback near the cursor for the whole inference window. Hidden on every `_process` exit (success/empty/exception) so it never sticks.
- **"Not ready" cue, NOT auto-queue** — a hotkey press during WARMING_UP/PROCESSING plays a distinct busy sound and is dropped. Auto-queuing the press was rejected: in a hold-to-talk model it would clip the start of the next utterance (recording can't start until the prior dictation finishes processing, but the user is already speaking).
- **Last-transcription menubar item is the paste-failure recovery affordance** — paste is a blind Cmd+V with no success check; clicking the item re-pastes `_last_text`. Failed transcriptions retain their audio for a "Retry last dictation" menu item.
- **Configurable model size is a non-goal** — SPEC.md lists "Fixed model" as a core principle and "Adaptive model selection" as excluded (consistent UX; `small.en` INT4 is fast enough everywhere). Confirmed with the owner 2026-06-16: not building a model picker.
- **`DICTATION_DEBUG=1`** — skips the fcntl lock and mirrors logs to the console for terminal debugging; the launchd process (no TTY, env unset) is unaffected.
- **Streaming transcription (transcribe-while-you-hold)** — `src/segmenter.py` closes speech segments at natural pauses (≥`STREAM_MIN_SEG_S`=8s of unsegmented audio + a quiet trailing window, or a `STREAM_MAX_SEG_S`=24s hard cap before Whisper's 30s window) during the hold, and transcribes each on a single serial worker (one M3 GPU). On release only the final tail remains, so release-to-text latency stops scaling with utterance length. Validated on a real 58s clip: release 1623ms→327ms (5.0x), assembled-vs-batch WER 0.000 (no seam loss). Short dictations (<8s) never segment → single tail == prior batch behavior (no risk, no benefit, by design). Each segment self-normalizes + runs its own VAD guard; `condition_on_previous_text=False` means no cross-segment repetition. Retry/recovery reconstructs full audio via `segmenter.full_audio()`.
- **Keep `small.en-q4` — no faster model wins on M3** (evaluated 2026-06-16): benchmarked distil-medium.en (SLOWER and less accurate — its full medium encoder dominates short clips), base.en-q4 (2.5x faster but ~5x word errors — botched "OAuth"/"Cloudflare"), and Parakeet tdt-0.6b-v2 (same accuracy, ~20% faster on short clips but SAME on a 58s clip: 1358 vs 1314ms, + new dependency + mangled "Postgres"). The decoder-pruning speedup the research promised is a large-model/server-GPU property that does not transfer to small.en INT4 on M3 short clips. `small.en-q4` is the accuracy champion (0.010 clean WER); the long-dictation latency win came from streaming, not a model swap. (Distinct from the locked "no model *picker*" non-goal — this is the choice of fixed model.)
