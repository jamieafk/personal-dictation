"""Audio capture via sounddevice. Records at native device rate, downsamples to 16kHz."""

import logging
import threading
from typing import List, Optional
import numpy as np
import sounddevice as sd

from src import config

log = logging.getLogger("dictation")

_chunks: List[np.ndarray] = []
_stream: Optional[sd.InputStream] = None
_lock = threading.Lock()
_rms_level: float = 0.0
_peak_level: float = 0.0
_had_errors: bool = False
_native_rate: int = 16000  # Actual device sample rate (may differ from 16kHz)
TARGET_RATE = 16000


def _find_builtin_mic() -> Optional[int]:
    """Find the built-in microphone device index.
    Avoids Bluetooth mics which cause audio profile switches on output."""
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and "MacBook" in d["name"]:
            return i
    return None


def _callback(indata, frames, time_info, status):
    global _rms_level, _peak_level, _had_errors
    if status:
        log.warning("Audio callback status: %s", status)
        _had_errors = True
    _chunks.append(indata.copy())
    _rms_level = float(np.sqrt(np.mean(indata**2)))
    block_peak = float(np.max(np.abs(indata)))
    if block_peak > _peak_level:
        _peak_level = block_peak


def _open_stream(device: Optional[int] = None) -> sd.InputStream:
    """Create an InputStream on the given device at its native sample rate."""
    global _native_rate
    if device is not None:
        info = sd.query_devices(device)
    else:
        info = sd.query_devices(kind="input")
    _native_rate = int(info["default_samplerate"])
    return sd.InputStream(
        device=device,
        samplerate=_native_rate,
        channels=1,
        dtype="float32",
        blocksize=1024,
        callback=_callback,
    )


def prepare():
    """Ensure an audio stream exists (stopped or active) for fast start.
    Call from a background thread after warmup. No-op if stream already exists."""
    global _stream
    with _lock:
        if _stream is not None:
            return  # Already exists (stopped or active)
        try:
            device = _find_builtin_mic()
            _stream = _open_stream(device)
            log.info("Audio stream pre-created (device=%s, rate=%d)",
                     device if device is not None else "default", _native_rate)
        except Exception as e:
            log.warning("Failed to pre-create audio stream: %s", e)
            _stream = None


def start_recording():
    """Start capturing audio. Uses pre-created stream if available,
    falls back to creating a new one."""
    global _stream, _rms_level, _peak_level, _had_errors
    with _lock:
        _chunks.clear()
        _rms_level = 0.0
        _peak_level = 0.0
        _had_errors = False

        # Fast path: start the pre-created stream
        if _stream is not None:
            try:
                _stream.start()
                return True
            except Exception as e:
                log.warning("Pre-created stream failed: %s", e)
                try:
                    _stream.close()
                except Exception:
                    pass
                _stream = None

        # Slow fallback: create + start (first recording or after prepare failure)
        try:
            device = _find_builtin_mic()
            _stream = _open_stream(device)
            _stream.start()
            return True
        except Exception as e:
            log.error("Failed to start recording: %s", e)
            _stream = None
            return False


def stop_recording() -> Optional[np.ndarray]:
    """Stop recording and return the audio buffer. Stream stays alive (stopped)
    for fast restart. Returns None if too short (<320ms)."""
    global _rms_level
    with _lock:
        if _stream is not None:
            try:
                _stream.stop()
            except Exception as e:
                log.warning("Error stopping audio stream: %s", e)

        if not _chunks:
            log.info("Recording discarded: no audio captured")
            return None

        raw = np.concatenate(_chunks, axis=0).flatten()
        _chunks.clear()
        _rms_level = 0.0

        # Min duration check at native rate
        min_samples = int(_native_rate * config.MIN_DURATION_S)
        if len(raw) < min_samples:
            log.info("Recording discarded: too short (%.0fms < %.0fms)",
                     len(raw) / _native_rate * 1000, config.MIN_DURATION_S * 1000)
            return None

        # Downsample to 16kHz if recorded at a higher rate
        if _native_rate != TARGET_RATE:
            num_target = int(len(raw) * TARGET_RATE / _native_rate)
            audio = np.interp(
                np.linspace(0, len(raw) - 1, num_target),
                np.arange(len(raw)),
                raw,
            ).astype(np.float32)
        else:
            audio = raw

        if _had_errors:
            log.warning("Audio had errors during recording — transcription may be degraded")

        return audio


def stop_stream():
    """Stop the recording stream and discard the captured buffer WITHOUT
    concatenating or downsampling. The streaming segmenter has already extracted
    every segment, so the raw buffer isn't needed here — this keeps the heavy
    array work off the main thread. Stream stays alive (stopped) for fast restart."""
    global _rms_level, _peak_level
    with _lock:
        if _stream is not None:
            try:
                _stream.stop()
            except Exception as e:
                log.warning("Error stopping audio stream: %s", e)
        _chunks.clear()
        _rms_level = 0.0
        _peak_level = 0.0


def get_rms_level() -> float:
    """Current audio RMS level (0.0-1.0). For UI visualization."""
    return _rms_level


def get_peak_level() -> float:
    """Running peak amplitude across the entire recording (0.0-1.0)."""
    return _peak_level


def native_rate() -> int:
    """Device sample rate of the active recording (samples/sec)."""
    return _native_rate


def samples_captured() -> int:
    """Total native-rate samples captured so far in the current recording.
    Used by the streaming segmenter to mark segment boundaries while recording
    is still active."""
    with _lock:
        return sum(len(c) for c in _chunks)


def recent_peak(window_s: float) -> float:
    """Max absolute amplitude over the trailing `window_s` of captured audio.
    Walks only the tail chunks (cheap). Used to detect a pause boundary. Reads a
    snapshot of the chunk list so concurrent capture is safe."""
    want = int(_native_rate * window_s)
    if want <= 0:
        return 0.0
    with _lock:
        chunks = list(_chunks)  # GIL-atomic snapshot; new appends are simply excluded
    peak, got = 0.0, 0
    for c in reversed(chunks):
        flat = c.reshape(-1)
        if flat.size:
            peak = max(peak, float(np.max(np.abs(flat))))
        got += flat.size
        if got >= want:
            break
    return peak


def extract_16k(start_native: int, end_native: int) -> np.ndarray:
    """Downsample captured native audio in [start_native, end_native) to 16kHz
    mono float32. Snapshots the buffer so concurrent capture is safe; segment
    boundaries are exact cumulative-sample indices, so consecutive extracts
    reconstruct the full recording with no gaps or overlaps."""
    with _lock:
        chunks = list(_chunks)
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    raw = np.concatenate(chunks, axis=0).reshape(-1)
    end_native = min(end_native, len(raw))
    start_native = max(0, min(start_native, end_native))
    seg = raw[start_native:end_native]
    if len(seg) == 0:
        return np.zeros(0, dtype=np.float32)
    if _native_rate == TARGET_RATE:
        return seg.astype(np.float32)
    num = int(len(seg) * TARGET_RATE / _native_rate)
    if num <= 0:
        return np.zeros(0, dtype=np.float32)
    return np.interp(
        np.linspace(0, len(seg) - 1, num),
        np.arange(len(seg)),
        seg,
    ).astype(np.float32)


def shutdown():
    """Close the audio stream. Call on app quit."""
    global _stream
    with _lock:
        if _stream is not None:
            try:
                _stream.stop()
                _stream.close()
            except Exception:
                pass
            _stream = None
