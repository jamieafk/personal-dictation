# 2026-06-16 — Latency: kill Whisper temperature-fallback tail spikes

## Goal
"Maximize performance" — reduce end-to-end dictation latency (hotkey release → text pasted) without regressing transcription quality.

## What We Did
Ran a multi-agent latency audit (verify mlx API + benchmark on M3 + quantify log history + sweep hot path + 3 adversarial quality reviewers), then shipped the one high-value change it surfaced.

**Finding (data-driven):** The decode path was already well-tuned (greedy, English pinned, fp16, no word-timestamps, model hot). The single remaining lever was Whisper's temperature-fallback schedule. mlx-whisper's default `temperature` is a **6-step tuple (0.0→1.0)**: when a 30s window trips a confidence/repetition threshold it silently re-decodes the whole window at the next temperature — up to 6 full decodes for one utterance.

Live-log evidence across **352 dictations**: timing is sharply bimodal — ~89% on a tight line (R²=0.98, ~10ms/word), then **25 dictations (7.1%) ran 2–13x slower for the same word count** (e.g. 4 words → 3229ms = 12.8x; 104 words → 8299ms = 6.8x). ~**90s cumulative wasted wall-clock**, all from fallback re-decodes.

**Change:** Pinned `temperature=(0.0, 0.2)` (plus thresholds at mlx defaults) in a shared `transcribe._DECODE_PARAMS`, used by both `warmup()` and `transcribe()`. Added telemetry that logs when the retry actually fires. Fixed the stale "~3ms" VAD comment (measured ~30ms on a 12s clip).

**Files:** `src/transcribe.py` (the change + telemetry + comment), `CLAUDE.md` (Decisions + Common Mistakes + VAD figure).

## Outcome
- Smoke test on real `say`-generated speech: accurate transcript, telemetry reads the real segment dicts safely, clean speech decoded at t=0.0 in one pass (avg_logprob −0.25, compression_ratio 1.18 — well inside thresholds).
- Full suite: **64/64 pass**.
- Deployed to live launchd app (`launchctl kickstart -k`); warmup clean, stable 60s+, zero errors in `~/.config/dictation/dictation.log`.

Effect: clean-speech latency unchanged (~240ms floor / ~390ms for 12s), but the occasional 3–8s freezes are capped at ~2x baseline instead of up to 13x.

## What We Learned
- **mlx-whisper's default temperature tuple is a latency trap.** The 6-step fallback is invisible — it only shows up as same-word-count time variance in logs. Pin a short schedule. (Now in CLAUDE.md Common Mistakes.)
- **Don't hard-disable fallback (scalar 0.0) on a quantized model.** Greedy argmax on INT4 logits can lock into a repetition loop; the worst case of scalar 0.0 is pasting repeating garbage at the cursor — a trust-damaging core-loop failure. `(0.0, 0.2)` keeps one cheap escape hatch and still captures ~all the latency win (clean speech never fires the retry). 3 independent reviewers converged here.
- **The 30s encoder window is a hard floor (~237ms).** Whisper pads every clip to 3000 mel frames; the positional embedding asserts on shape, so you can't shrink the window without a different model. Short-clip latency is already near-optimal.
- **Deferred (measured, low-impact, sensitive paths):** downsample runs on the main run loop (5–17ms, only material >60s recordings); the 150ms `PASTE_DELAY_S` sits on the "ready for next dictation" path (not on text-appears). Both have ready fix recipes in the audit but were judged not worth churning trust-sensitive recording/clipboard paths for marginal gains.
- **Biggest theoretical lever = smaller model** (base.en/tiny.en ≈ 2–4x faster), but that's a direct accuracy hit and a locked non-goal (fixed `small.en`, confirmed 2026-06-16). Not implemented — surfaced for owner decision only.
