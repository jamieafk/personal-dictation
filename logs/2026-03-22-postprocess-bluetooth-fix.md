# Goal

Add text post-processing (vocab dict + filler removal) inspired by the Handy project, and fix a bug where dictating caused volume changes on AirPods.

# What We Did

- Explored github.com/cjpais/Handy to learn from their text processing pipeline
- Built `src/postprocess.py` with three stages: fuzzy vocabulary matching (Levenshtein + Soundex + n-gram), filler word removal (15 English filler words via compiled regex), stutter collapse (3+ repeats of 1-2 char words)
- Vocab loaded from `~/.config/dictation/vocab.txt` (one correct word per line, comments with `#`, live-reloaded each dictation)
- Integrated into `app.py._process()` between transcription and paste
- Diagnosed volume change issue: opening AirPods mic via sounddevice/PortAudio forced macOS Bluetooth profile switch from A2DP (hi-fi stereo) to HFP (phone quality), changing perceived volume
- Verified scientifically: `system_profiler` showed AirPods output sample rate dropping from 48kHz to 24kHz during mic use
- Fixed by preferring built-in MacBook mic (`_find_builtin_mic`) and recording at native device sample rate with `np.interp` downsample to 16kHz
- Ran drift-guard: updated CLAUDE.md (module count, architecture table, two new decisions), FEATURES.md (four new features), `audio.py` docstring

# Outcome

64 tests pass (27 new). Three features shipped. Bluetooth audio quality no longer affected during dictation. Text output is cleaner with fillers removed and vocab corrections applied.

# What We Learned

- macOS Bluetooth audio has two profiles: A2DP (output-only, high quality) and HFP (bidirectional, low quality). Opening a mic on a Bluetooth device forces the switch. Solution: use a non-Bluetooth mic.
- sounddevice/PortAudio records at whatever sample rate you specify, potentially reconfiguring hardware. Recording at native rate avoids hardware changes.
- Handy's fuzzy matching uses Levenshtein + Soundex + n-gram windows — ported to Python using stdlib `difflib.SequenceMatcher` and a 20-line Soundex implementation (no new dependencies).
- User corrected approach: "validate your hypothesis before you try to apply a fix" — don't jump to implementation on suspected causes, gather diagnostic data first.
