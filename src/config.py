"""Single source of truth for config paths and behavioral tunables.

Every module that needs the config directory or a tunable threshold imports it
from here, so a path rename or a tuning tweak happens in exactly one place.
Pure-stdlib and dependency-free so it can be imported from anywhere without
risk of circular imports.
"""

import os

# Directory for all persistent state: logs, history, vocab, lifetime counter, lock.
CONFIG_DIR = os.path.expanduser("~/.config/dictation")

# --- Behavioral tunables ---
# Clipboard restore delay after paste. Conservative for Electron apps (Slack,
# VS Code, Discord) that read the pasteboard asynchronously.
PASTE_DELAY_S = 0.15

# Delay before re-pasting from the menubar item, so the previously-focused app
# has time to regain key focus after the menu dismisses (non-deterministic OS
# handoff — match the conservative posture of PASTE_DELAY_S).
REPASTE_FOCUS_DELAY_S = 0.15

# Audio normalization (see transcribe._normalize).
GAIN_CAP = 100.0               # max gain — avoids amplifying the noise floor
NORMALIZE_SILENCE_FLOOR = 0.001  # below this peak it's silence; don't amplify
NORMALIZE_THRESHOLD = 0.1      # below this peak, boost toward NORMALIZE_TARGET
NORMALIZE_TARGET = 0.5         # target peak after boost

# Recording boundaries.
MIN_DURATION_S = 0.32          # discard recordings shorter than this (~quick tap)
HOLD_CANCEL_S = 0.3            # hold shorter than this is a cancel, not a dictation

# Post-processing.
VOCAB_THRESHOLD = 0.18         # fuzzy vocab match distance (lower = stricter)

# Overlay animation.
VAD_SPEAKING_FLOOR = 0.002     # RMS above this reads as "speaking" for the bars

# Re-run a tiny warm-up decode on hotkey press when the model has been idle this
# long (see transcribe.rewarm_if_idle). The cold penalty shows up from ~5–30 min.
REWARM_IDLE_S = 600.0

# GPU memory MLX keeps wired (unpageable). Weights are ~200MB, but each decode runs
# a fixed 30s window: measured peak ~940MB (2026-10-09), so 1GB covers it all.
MLX_WIRED_LIMIT_MB = 1024

# How often to re-check for Accessibility permission once the hotkey is disabled.
HOTKEY_RECHECK_S = 4.0
# If the tap has delivered no key events this long after it was enabled, the
# Input Monitoring grant is probably stale (e.g. after a py2app rebuild): warn in the log.
HOTKEY_SILENT_WARN_S = 60.0

# Streaming transcription (transcribe-while-you-hold). The segmenter closes a
# speech segment at a natural pause once enough unsegmented audio has accrued,
# transcribes it in the background during the hold, and on release only the
# final tail remains — decoupling release-to-text latency from utterance length.
STREAM_MIN_SEG_S = 8.0         # don't close a segment until this much unsegmented audio has accrued
STREAM_MAX_SEG_S = 24.0        # force-close even mid-speech before Whisper's 30s window (avoids a 2nd window)
STREAM_SILENCE_PEAK = 0.02     # native peak below this over the trailing window reads as a pause boundary
STREAM_SILENCE_WIN_S = 0.45    # trailing window that must be quiet to mark a pause
STREAM_POLL_S = 0.1            # how often the segmenter checks for a pause (cheap; release wakes it)

# Speculative tail (see segmenter): at a pause this long, pre-transcribe the pending
# audio so a release right after it pastes without decoding. Shorter = more hits but
# more wasted decodes at mid-sentence pauses. False disables it.
STREAM_SPECULATE = True
STREAM_SPEC_SILENCE_S = 0.3

# Sound within this long before release means the user was still speaking: wait this
# long for in-flight input buffers (1024-frame block + device latency) so the last
# syllable isn't clipped. A quiet release ends the tail exactly at key-up instead.
RELEASE_GRACE_S = 0.06
