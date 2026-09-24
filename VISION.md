# Vision

## Goal
Hold a key, speak, release: clean text lands in whatever app has focus. It replaces WisprFlow with no subscription and no voice sent to the cloud.

## Target user
Me, on an Apple Silicon Mac, dictating all day into terminals, browsers, and chat apps.

## Success criteria
1. **Daily driver.** Reliable enough that I never reach for another dictation tool.
2. **Fast.** Short dictations feel instant (sub-500ms from release to paste), and long ones don't scale with length.
3. **Zero ongoing cost.** All inference runs locally.
4. **No privacy compromise.** Audio never leaves the machine, and the mic is live only while the key is held.
5. **Low maintenance.** Survives macOS updates without babysitting.

## Constraints (priority order)
1. Privacy: no network calls at runtime, no always-on mic, no telemetry or implicit data collection.
2. Speed.
3. Accuracy: `small.en` quality plus personal vocabulary fixes.
4. Low maintenance: Python-only, minimal dependencies.

## Non-goals
- Model picker or adaptive model selection (fixed `small.en-q4`, see CLAUDE.md Decisions).
- LLM cleanup pass (latency cost outweighs the benefit).
- Always-on mic or wake word.
- Learning from corrections (implicit data collection).
- Cross-platform or multi-user support.

Original design rationale and open questions: `SPEC.md` (historical; the Python-only MVP path won, and streaming by pause segmentation later replaced the "no streaming" exclusion).
