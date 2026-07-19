# Goal

Fix the ~1s delay between pressing Right Option and the app responding (sound, overlay, recording start).

# What We Did

Traced the delay to `audio.start_recording()` creating a new PortAudio stream on the main thread every keypress. Fixed by:

- Added `audio.prepare()` to pre-create the stream at idle time (after warmup, after each transcription, after cancel)
- Reordered `_on_press` to fire sound + overlay before `start_recording()`
- Added slow fallback path if pre-created stream fails

# Outcome

Keypress response is now near-instant. 37 tests still pass. The pre-created stream is opened but not started, so no privacy concern (no mic indicator until recording actually begins).

# What We Learned

On macOS, sounddevice/PortAudio `InputStream` creation (`Pa_OpenStream`) involves device enumeration and buffer allocation that takes ~500ms-1s. `Pa_StartStream` on an already-opened stream is near-instant. Pre-creating the stream at idle time is the right pattern for latency-sensitive audio apps.
