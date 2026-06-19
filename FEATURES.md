# Features

- **Instant long dictations (transcribe-while-you-talk)** (2026-06-16) — Long dictations land almost as fast as short ones. While you hold the key, the app quietly transcribes each chunk of speech at natural pauses, so on release only the last few words remain.
  - Measured on a 58-second dictation: time from release to pasted text dropped from 1.6s to 0.33s (5× faster), with word-for-word identical accuracy.
  - Short dictations (under ~8s) are unchanged — they were already instant.
  - Uses the same (most accurate) model; a benchmark of faster models (distilled Whisper, NVIDIA Parakeet) found none that were faster *and* as accurate on this Mac.
  - Why: A dictation tool that stalls for seconds after a long thought feels broken; latency should track how fast you read it back, not how long you spoke.

- **Snappier transcription (no more random freezes)** (2026-06-16) — Eliminated the occasional multi-second stalls where the same dictation would take 3–13× longer for no visible reason.
  - Root cause: Whisper silently re-transcribed audio up to 6× on low-confidence guesses. Capped to at most 2 passes, keeping one safety retry for the rare garbled clip.
  - Why: Unpredictable multi-second waits on the core action erode trust.

- **Hold-to-talk dictation** (2026-03-22) — Hold Right Option, speak, release. Transcribed text appears in the focused app.
  - Local-only via mlx-whisper (small.en INT4 quantized). ~190ms inference on 3s of audio.
  - Clipboard saved before paste and restored after, preserving RTF/images.
  - 300ms cancel threshold — quick taps are ignored.
  - Why: Replace WisprFlow with a zero-cost, fully private alternative.

- **Transcription history** (2026-03-22) — Every dictation is saved with timestamp, focused app name, and full text.
  - Scrollable history window with search, pagination (50 per page), and export to file.
  - Copy buttons on each entry with "Copied!" confirmation. Text is selectable.
  - Delete individual entries (3-second undo window) or Delete All (with confirmation dialog). Fully removes from disk.
  - Auto-refreshes when new dictations arrive. Scrolls to most recent on open.
  - Total word count displayed in the header. Lifetime word counter in menubar survives deletions.
  - Why: Reference past dictations, recover text if paste fails. Delete for privacy.

- **Recording indicator** (2026-03-22) — Small floating overlay at bottom of screen with animated neon purple bars during recording.
  - Bars dance when speaking, settle when silent. Binary animation, not volume-dependent.
  - Non-focus-stealing — stays on top without interrupting the focused app.
  - Why: Visual confirmation that the app is listening.

- **Sound feedback** (2026-03-22) — Short "tink" sound plays on both press and release of the hotkey.
  - No sound on cancel (quick tap <300ms).
  - Why: Tactile confirmation the hotkey was recognized.

- **Auto-launch on login** (2026-03-22) — App starts automatically when you log in and restarts if it crashes.
  - Built as a proper macOS .app via py2app. Managed by launchd Launch Agent.
  - Single-instance guard prevents duplicate menubar icons.
  - Install: `./install.sh`. Uninstall: `./uninstall.sh`.
  - Why: Always-on dictation without manual terminal launches.

- **Discard feedback** (2026-03-22) — When dictation is filtered out (no useful speech detected), the overlay briefly flashes orange.
  - Distinguishes "nothing to transcribe" from "something broke."
  - Why: Prevents confusion when the app intentionally discards audio.

- **Escape to cancel** (2026-03-22) — Press Escape while recording to cancel and discard.
  - Cleaner than quick-releasing within 300ms.
  - Why: Bail out when you start recording in the wrong context.

- **Instant hotkey response** (2026-03-22) — Pressing Right Option triggers sound and overlay immediately with no perceptible delay.
  - Audio stream pre-created at idle time so recording starts near-instantly.
  - Why: PortAudio stream creation was blocking the main thread for ~1s on each keypress.

- **Text cleanup** (2026-03-22) — Transcription output is automatically cleaned before pasting.
  - Filler word removal: strips "uh", "um", "hmm", and 12 other English fillers.
  - Stutter collapse: "I I I I think" becomes "I think" (3+ repeats of short words).
  - Why: Whisper outputs raw speech including verbal tics — cleanup makes dictated text read naturally.

- **Custom vocabulary** (2026-03-22) — Fuzzy-match correction for proper nouns and jargon Whisper misspells.
  - Add words to `~/.config/dictation/vocab.txt`, one per line. Edit live, no restart needed.
  - Levenshtein + Soundex phonetic matching with n-gram support ("car pathy" → "Karpathy").
  - Why: Whisper doesn't know your people, products, or technical terms.

- **Bluetooth-safe recording** (2026-03-22) — Uses the built-in MacBook mic instead of Bluetooth mic to avoid disrupting AirPods audio.
  - Prevents the A2DP → HFP Bluetooth profile switch that changes perceived volume during playback.
  - Why: Opening a mic on AirPods forces macOS to downgrade audio output quality.

- **Live processing indicator** (2026-06-16) — After you release, the floating pill stays on screen as a neon-purple pulse until the text actually pastes.
  - Previously the overlay vanished at release and the only cue was the menubar ellipsis, off in the corner.
  - The pill is reliably hidden on every outcome (success, nothing-detected, error) so it never gets stuck visible.
  - Why: On longer dictations there was a 2–8s window with no feedback near the cursor — you couldn't tell if release registered.

- **Re-paste the last dictation** (2026-06-16) — Click the most-recent transcription in the menubar to paste it again into the focused app.
  - Primary use: recovery when the original paste landed nowhere (a non-text field, focus moved, an Electron app swallowed it). The text is always in history, but this makes recovery one click instead of opening the history window.
  - Why: Paste is a blind Cmd+V with no success check — this is the safety net for the worst-case "my words vanished."

- **Retry a failed dictation** (2026-06-16) — If transcription errors out, the audio is kept and a "Retry last dictation" menu item re-runs it — no need to re-speak.
  - Greyed out until a failure happens; the error notification points you to it.
  - Why: A transient model error used to discard your words permanently with no recovery.

- **Honest hotkey status + one-click fix** (2026-06-16) — When Accessibility permission is missing, the menubar shows a ⚠️ warning (not the "ready" mic) plus a "Hotkey disabled — grant Accessibility" item that opens the right System Settings pane. It re-enables itself automatically once you grant permission — no restart.
  - Why: The menubar used to show "ready" while hold-to-talk was silently dead, e.g. after a macOS update revoked the grant.

- **"Not ready" cue** (2026-06-16) — Pressing the hotkey while the model is still warming up, or while the previous dictation is still processing, plays a distinct sound instead of silently doing nothing.
  - Releasing too fast (a clipped quick-tap) now flashes the discard indicator instead of giving no feedback at all.
  - Why: A press that didn't register used to be invisible, so a whole spoken sentence could be lost without the user realizing.
