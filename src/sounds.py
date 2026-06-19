"""Audio feedback for recording start/stop. Uses macOS system sounds."""

from AppKit import NSSound

_SOUND_PATH = "/System/Library/Sounds/Tink.aiff"
# Distinct, clearly-different "not now" cue for a hotkey press that can't start a
# recording yet (model still warming up, or a previous dictation still processing).
_BUSY_SOUND_PATH = "/System/Library/Sounds/Funk.aiff"

# Preload at import time to avoid disk I/O latency on first play
_sound = NSSound.alloc().initWithContentsOfFile_byReference_(_SOUND_PATH, True)
_busy_sound = NSSound.alloc().initWithContentsOfFile_byReference_(_BUSY_SOUND_PATH, True)


def play_start():
    """Play the recording-started feedback sound. Non-blocking."""
    if _sound:
        _sound.stop()
        _sound.play()


def play_stop():
    """Play the recording-stopped feedback sound. Non-blocking."""
    if _sound:
        _sound.stop()
        _sound.play()


def play_busy():
    """Play the 'not ready' cue when a press can't start a recording. Non-blocking.

    Deliberately a different sound from start/stop so the user can tell a rejected
    press apart from a successful one rather than speaking into the void."""
    if _busy_sound:
        _busy_sound.stop()
        _busy_sound.play()
