"""Whisper inference via mlx-whisper. Model kept hot in memory."""

import logging
import os
import numpy as np
import torch
import mlx_whisper
from silero_vad import load_silero_vad, get_speech_timestamps

from src import config

log = logging.getLogger("dictation")

# Use bundled model (no internet needed). Falls back to HuggingFace if missing.
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCAL_MODEL = os.path.join(_PROJECT_DIR, "models", "whisper-small.en-mlx-q4")
MODEL_PATH = _LOCAL_MODEL if os.path.isdir(_LOCAL_MODEL) else "mlx-community/whisper-small.en-mlx-q4"
_model_loaded = False
_vad_model = None

# Decode parameters shared by warmup() and transcribe() so the warmed-up code
# path is identical to the live one.
#
# temperature=(0.0, 0.2): mlx-whisper's default is a 6-step fallback schedule
# (0.0→1.0). When a decoded 30s window trips a confidence/repetition threshold it
# silently RE-DECODES the whole window at the next temperature — up to 6 full
# decodes for one utterance. Measured against the live log: ~7% of dictations hit
# this and ran 2–13x slower for the same word count (~90s cumulative waste). A
# 2-step schedule caps the worst case at 2 decodes instead of 6 — killing ~80% of
# that tail — while keeping ONE cheap retry. That retry matters because this model
# is INT4-quantized: greedy argmax on quantized logits can occasionally lock into
# a repetition loop, and the t=0.2 resample is the escape hatch. Clean speech (the
# overwhelming majority) passes at t=0.0 on the first decode and never pays for the
# retry, so normal-dictation latency is unchanged. Thresholds are pinned at the
# mlx-whisper defaults so (a) the retry only fires on a genuinely repetitive
# (compression_ratio>2.4) or low-confidence (avg_logprob<-1.0) decode, and (b) the
# behavior is version-stable instead of inheriting an implicit upstream default.
_DECODE_PARAMS = dict(
    language="en",
    condition_on_previous_text=False,
    temperature=(0.0, 0.2),
    compression_ratio_threshold=2.4,
    logprob_threshold=-1.0,
    no_speech_threshold=0.6,
)


def _normalize(audio: np.ndarray, precomputed_peak: float = 0.0) -> np.ndarray:
    """Boost quiet audio before inference. Caps gain at 100x to avoid amplifying noise."""
    if len(audio) == 0:
        return audio
    peak = precomputed_peak if precomputed_peak > 0 else float(np.max(np.abs(audio)))
    if peak < config.NORMALIZE_SILENCE_FLOOR:
        return audio  # Silence — don't amplify noise floor
    if peak < config.NORMALIZE_THRESHOLD:
        gain = min(config.NORMALIZE_TARGET / peak, config.GAIN_CAP)
        return audio * gain
    return audio


def _has_speech(audio: np.ndarray) -> bool:
    """Check if audio contains speech using Silero VAD. ~30ms on a ~12s clip
    (scales with clip length) — runs on the critical path before inference."""
    global _vad_model
    if _vad_model is None:
        _vad_model = load_silero_vad(onnx=True)
    tensor = torch.from_numpy(audio)
    timestamps = get_speech_timestamps(tensor, _vad_model, sampling_rate=16000)
    return len(timestamps) > 0


def warmup():
    """Run a silent sample through the model to JIT-compile MLX kernels."""
    global _model_loaded, _vad_model
    silent = np.zeros(16000, dtype=np.float32)  # 1 second of silence
    mlx_whisper.transcribe(silent, path_or_hf_repo=MODEL_PATH, **_DECODE_PARAMS)
    _model_loaded = True
    # Also warm up VAD
    _vad_model = load_silero_vad(onnx=True)


def transcribe(audio: np.ndarray, precomputed_peak: float = 0.0) -> str:
    """Transcribe a float32 16kHz audio array. Returns empty string if no speech detected."""
    if len(audio) == 0:
        log.info("Discarded: empty audio buffer")
        return ""

    # VAD on raw audio BEFORE normalization — Silero was trained on real mic levels.
    # Running VAD after gain boost causes amplified noise to trigger false positives.
    if not _has_speech(audio):
        log.info("Discarded: VAD found no speech (silent or too quiet)")
        return ""

    audio = _normalize(audio, precomputed_peak)

    result = mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL_PATH, **_DECODE_PARAMS)

    # Telemetry: log when the fallback retry actually fired (a segment decoded at
    # t>0). If this proves to essentially never fire in real use, the schedule can
    # be dropped to a single greedy pass (temperature=0.0) with data behind it.
    escalated = [s for s in (result.get("segments") or [])
                 if s.get("temperature", 0.0) > 0.0]
    if escalated:
        worst = max(escalated, key=lambda s: s.get("temperature", 0.0))
        log.info("Fallback retry fired (t=%.1f, compression_ratio=%.2f, avg_logprob=%.2f)",
                 worst.get("temperature", 0.0),
                 worst.get("compression_ratio", 0.0),
                 worst.get("avg_logprob", 0.0))

    text = result["text"].strip()
    if not text:
        log.info("Discarded: Whisper returned empty transcript")
    return text
