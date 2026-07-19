# 2026-06-16 — Streaming transcription (transcribe-while-you-hold) + model evaluation

## Goal
"Maximize performance" → after shipping the temperature-fallback fix, the owner chose "go all-in on long-dictation speed": a faster model AND re-architecting so the app transcribes during the hold.

## What We Did

### 1. Model evaluation (result: keep current model)
Benchmarked candidates on M3 against the current `small.en-q4`, latency + WER on a fixed 11-case ground-truth set + a 58s clip:
- **distil-medium.en** — 527ms mean vs 264ms (2x SLOWER) and clean-WER 0.032 vs 0.010. Its full medium encoder dominates short clips; the 2-layer decoder doesn't help when the encoder is the fixed cost. Dud on this hardware.
- **base.en-q4** — 103ms mean (2.5x faster) but clean-WER 0.047 (~5x more errors; botched "OAuth", "Cloudflare"). Speed-for-accuracy trade.
- **Parakeet tdt-0.6b-v2** (isolated venv, new dep) — same accuracy (0.010), ~20% faster on short clips (imperceptible), SAME on 58s clip (1314 vs 1358ms), mangled "Postgres". Not worth an engine swap.
- **Conclusion:** the research's "decoder-pruned distil = 5x" is a large-model/server-GPU property; doesn't transfer to small.en INT4 on M3 short clips. Kept `small.en-q4` (accuracy champion). The long-dictation win had to come from architecture, not the model.

### 2. Streaming architecture (the real win)
`src/segmenter.py` `StreamingSegmenter`: a poll thread closes a speech segment at a natural pause (≥`STREAM_MIN_SEG_S`=8s unsegmented + quiet trailing window, or `STREAM_MAX_SEG_S`=24s cap) and a single serial worker transcribes each during the hold. On release only the final tail remains.
- audio.py: added `samples_captured`/`recent_peak`/`extract_16k`/`stop_stream` (segment boundaries are exact cumulative native indices → gap-free reconstruction; `stop_stream` keeps the downsample off the main thread).
- app.py: press starts the segmenter; release runs the min-duration check on the main thread then spawns `_finalize_streaming` OFF the main thread (the CGEvent tap must never block on the poll-thread join). Per-segment VAD guard + retry preserved (retry reconstructs full audio).
- Dependency-injected audio source + transcribe fn → fully unit-testable (10 new tests incl. a threaded integration test).

## Outcome
- **End-to-end validation on a real 58s clip with the production model: release latency 1623ms → 327ms (5.0x faster), assembled-vs-batch WER 0.000 (no seam loss).** 5 segments closed at pauses during the hold; only the 5.4s tail transcribed on release.
- 74/74 tests pass. Live app redeployed (launchd kickstart), clean warmup, no errors.
- Short dictations unchanged (single tail == prior batch behavior).

## Incident: worktree symlink clobbered .venv/models
Built streaming in a git worktree sharing the real `.venv`/`models` via symlinks. `.gitignore` used trailing-slash patterns (`.venv/`) which match a *directory* but NOT a symlink of the same name, so `git add -A` staged the self-referential symlinks; merging replaced the real dirs with broken links.
- **Fix:** untracked them, removed the broken links, tightened `.gitignore` to slash-less `.venv`/`models`, rebuilt the venv (`/usr/bin/python3` = 3.9.6, `pip install -r requirements.txt`) and re-downloaded the model. ~5 min, fully recovered, 74/74 pass on the rebuilt env.
- The live app survived throughout (it had loaded everything into memory before the merge).

## What We Learned
- **Benchmark model claims on the actual target.** Three "faster" models all failed to beat current at equal accuracy on M3 short clips — the opposite of the published numbers. Validation saved a pointless, risky engine swap. (Now a CLAUDE.md Decision.)
- **The median dictation is already at the floor; only architecture moves long ones.** Encoder/VAD/dispatch dominate short clips; transcribing during the hold is the only lever that decouples latency from length.
- **In a worktree, never `git add -A`** — and `.gitignore` dir patterns don't catch same-named symlinks. (Now a CLAUDE.md Common Mistake.)
- **The release path is the CGEvent tap callback** — joining threads there blocks the run loop; finalize off-thread. (Now a CLAUDE.md Common Mistake.)

## Untested (only the owner can, on real hardware)
Real-mic dictation via the actual hotkey — specifically whether the audio callback drops frames while the worker holds the GPU during the hold. The synthetic playback can't exercise the real PortAudio-vs-GPU concurrency. Watch `~/.config/dictation/dictation.log` for "Audio callback status" warnings on first real long dictation.
