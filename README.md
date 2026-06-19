# Personal Dictation

A local-only, zero-cost dictation tool for macOS. **Hold Right Option, speak, release — your words appear in whatever app is focused.** Everything runs on-device: no cloud, no subscription, no telemetry, no account.

It's a private, free alternative to cloud dictation tools like WisprFlow, built around Whisper running locally on Apple Silicon.

## Why

Cloud dictation works, but it costs money every month and sends your voice to someone else's servers. This does the same core thing — fast, push-to-talk dictation anywhere on macOS — with none of that. Audio never leaves your machine, and the microphone is only ever live while you're holding the key.

## Features

- **Hold-to-talk** — hold Right Option, speak, release. Transcribed text is pasted into the focused app.
- **Fast, and fast even on long dictations** — short clips land in a few hundred milliseconds. Long ones transcribe *while you talk* (at natural pauses), so the wait after you release stays short no matter how long you spoke.
- **Fully private & offline** — local Whisper inference via [MLX](https://github.com/ml-explore/mlx). No network calls, no always-on mic, no data collection.
- **Hallucination guard** — Silero VAD discards audio with no real speech, so you never get Whisper's "thank you for watching" on silence.
- **Text cleanup** — strips filler words ("um", "uh"), collapses stutters, and applies a custom vocabulary file for names/jargon Whisper tends to misspell.
- **Transcription history** — every dictation is saved locally with timestamp and app name, searchable, with re-paste and delete.
- **Recording & processing overlay** — a small neon waveform near the cursor shows when it's listening and when it's working.
- **Auto-launch & crash recovery** — runs as a background menubar app, starts on login, restarts if it crashes.
- **Graceful failure** — re-paste the last dictation if a paste lands nowhere, retry a failed transcription without re-speaking, and an honest menubar warning (with one-click fix) if Accessibility permission is missing.

See [FEATURES.md](FEATURES.md) for the full list.

## Requirements

- An **Apple Silicon Mac** (M1 or later) — inference runs on MLX, which is Apple-Silicon only
- **macOS 13+**
- **Python 3.9+**

## Install

```bash
# 1. Clone and enter the project
git clone https://github.com/jamieafk/personal-dictation.git
cd personal-dictation

# 2. Create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Build the macOS .app bundle
python setup.py py2app -A

# 4. Install auto-launch (starts on login, restarts on crash)
./install.sh
```

The Whisper model (`mlx-community/whisper-small.en-mlx-q4`, ~180MB) downloads automatically from Hugging Face the first time you dictate.

> **Note:** The build uses py2app *alias mode* — the `.app` symlinks back to this source folder, so keep the project directory in place after installing. To stop auto-launch, run `./uninstall.sh`.

## Permissions

macOS will prompt for two permissions the first time. Grant both to **Personal Dictation** in **System Settings → Privacy & Security**:

- **Accessibility** — for the global hotkey and to paste into apps
- **Microphone** — to capture your speech

## Usage

- **Dictate:** hold **Right Option**, speak, release. Text is pasted at your cursor.
- **Cancel:** press **Escape** while holding (or release within ~300ms).
- **History, re-paste, retry, quit:** all in the menubar icon's dropdown.

### Custom vocabulary

Whisper doesn't know your people, products, or jargon. Add corrections to `~/.config/dictation/vocab.txt`, one term per line — edits take effect live, no restart needed. It uses fuzzy phonetic matching, so `car pathy` becomes `Karpathy`.

Local files the app uses:

| Path | Purpose |
|------|---------|
| `~/.config/dictation/vocab.txt` | Your custom vocabulary corrections |
| `~/.config/dictation/history.txt` | Plain-text dictation history |
| `~/.config/dictation/dictation.log` | App log |

## Architecture

A Python menubar app (rumps) with one module per responsibility under `src/`:

| Module | Responsibility |
|--------|---------------|
| `app.py` | Menubar app, state machine, orchestrator |
| `hotkey.py` | CGEvent tap for Right Option hold-to-talk |
| `audio.py` | Microphone capture and downsampling to 16kHz |
| `transcribe.py` | mlx-whisper inference and model lifecycle |
| `segmenter.py` | Streaming: transcribe speech chunks during the hold |
| `postprocess.py` | Text cleanup: vocabulary, filler removal, stutter collapse |
| `paste.py` | Clipboard save/restore + paste into the focused app |
| `overlay.py` | Floating waveform indicator |
| `history.py` / `history_window.py` | History persistence and browser |
| `sounds.py` | Audio feedback on press/release |
| `config.py` | Central config: paths and behavioral tunables |

Run the test suite with `python tests/run_tests.py`.

## Privacy

- Audio is captured only while the hotkey is held, processed entirely on-device, and never written to disk or sent anywhere.
- The only data persisted is the transcribed text (in your local history file), which you can search and delete from the app.
- No analytics, no telemetry, no network calls for inference.

## License

[MIT](LICENSE)
