# Personal Dictation Tool — Design Spec

**Date:** 2026-03-15
**Status:** Draft

---

## Objectives

### What we're building
A local-only, zero-cost macOS dictation tool for personal use. Hold a key, speak, release — cleaned text appears in the focused app. Everything runs on-device. No subscriptions, no cloud APIs, no telemetry.

### Why
WisprFlow works but costs money and sends your voice to the cloud. We want the same core experience — hold-to-talk dictation that works anywhere on macOS — without the subscription or the privacy trade-off.

### Success criteria
1. **Daily driver within one week.** If it's not reliable enough to replace WisprFlow within a week of building Phase 1, something is wrong with the design.
2. **Faster than WisprFlow for typical use.** Short-to-medium dictations (1-10 seconds) should feel at least as fast. We don't need to beat them on every dimension — just on the use patterns that matter most.
3. **Zero ongoing cost.** No cloud APIs, no subscriptions. All inference runs locally on Apple Silicon.
4. **No privacy compromise.** Audio never leaves the machine. No telemetry, no analytics, no implicit data collection. The mic is only active while the hotkey is held.
5. **Low maintenance.** Simple enough that it doesn't break when macOS updates, doesn't need babysitting, and doesn't accumulate technical debt.

### Design priorities (ordered)
1. Privacy — fully local, no network calls, no always-on mic, no implicit data collection
2. Speed — consistent sub-500ms latency, beating WisprFlow's cloud round-trip
3. Accuracy — `small.en` quality with personal vocabulary corrections
4. Low maintenance — simple architecture, minimal dependencies, no fragile heuristics

---

## Current Plan

### Architecture

**Two-layer design:**

- **Swift menubar app** — handles menubar icon, global hotkey via `CGEvent` tap, mic/accessibility permission management, audio level display, and recording state UI. Communicates with the Python process over a Unix socket.
- **Python inference process** — owns audio capture (`sounddevice`), Whisper inference (`mlx-whisper`), post-processing, and text insertion. Keeps the model hot in memory. Launched by the Swift app at startup.

**Why this split:** The Swift layer gives us a proper `.app` bundle with clean permission grants (no "grant Accessibility to Python.app" confusion). The Python layer keeps us in the MLX ecosystem where Whisper inference lives. This is the architecture production dictation tools actually use.

**Pragmatic alternative for MVP:** Start with Python-only using PyObjC `CGEvent` tap for hotkey and `rumps` for menubar. Replace the shell layer once the core loop is validated.

### Core Flow

```
1. User holds global hotkey (Right Option)
2. Recording starts — audio captured via sounddevice (16kHz mono) into memory buffer
3. Menubar icon shows recording state
4. User releases hotkey
5. Recording stops
6. Silero VAD checks: did the buffer contain speech?
   - No speech detected → discard, do nothing (hallucination guard)
   - Speech detected → continue
7. Full audio buffer sent to mlx-whisper (small.en, INT4, greedy decoding, language="en")
8. Post-processing: vocabulary string replacements applied
9. Current clipboard contents saved via NSPasteboard
10. Transcript placed on clipboard
11. AppleScript pastes via Cmd+V into focused app
12. Previous clipboard contents restored (after 150ms delay)
```

### Components

**Global Hotkey**
- `CGEvent` tap via PyObjC (MVP) or Swift (final)
- Hold-to-talk: recording starts on key down, stops on key up
- Cancel: Escape while holding, or release within <300ms
- Default: Right Option (avoid Fn/Globe — macOS 13+ intercepts it)

**Audio Capture**
- `sounddevice`, 16kHz mono, in-memory numpy buffer
- Menubar dropdown for input device selection (Phase 2)

**Speech-to-Text**
- `mlx-whisper`, `small.en` INT4 quantized
- `language="en"` (skips language detection, ~100ms savings)
- Greedy decoding (default, `beam_size` is not supported in mlx-whisper)
- `initial_prompt` for punctuation/formatting bias only — vocabulary corrections handled in post-processing because `initial_prompt` influence is unreliable
- Model loaded once at startup, kept resident in memory

**Hallucination Guard**
- Silero VAD with ONNX runtime backend (not PyTorch — avoids ~2GB dependency)
- If VAD reports no speech, skip inference entirely
- Catches Whisper's tendency to hallucinate on silence ("Thank you for watching")

**Post-Processing**
- User-maintained vocabulary replacement file (`car pathy -> Karpathy`)
- Simple string replacement. No regex filler removal (fragile), no LLM cleanup (adds latency for negligible benefit)

**Text Insertion**
- `NSPasteboard` via PyObjC for full clipboard save/restore (plain text, RTF, images)
- AppleScript `keystroke "v" using command down` for paste
- 150ms delay before clipboard restore (conservative for Electron apps)
- Fallback: if paste fails, transcript stays on clipboard for manual Cmd+V

**Menubar UI**
- States: idle, warming up, recording, processing, error
- Dropdown: last transcription, input device selector, vocabulary file, history file, quit

**Dictation History**
- Plain text, one entry per line: `2026-03-15 14:32:05 | transcribed text here`
- Location: `~/.config/dictation/history.txt`
- `grep` is the query engine

### Latency Budget

| Step | Time (M2/M3 Pro) | Notes |
|------|------------------|-------|
| Audio buffer flush | 5-10ms | Drain last sounddevice buffer |
| VAD check | 5-10ms | Silero on full buffer |
| Mel spectrogram | 10-20ms | Internal to mlx-whisper transcribe() |
| Whisper inference | 150-400ms | 3s clip ≈ 150ms, 10s clip ≈ 400ms |
| Post-processing | 1-2ms | String replacements |
| Clipboard + paste | 30-50ms | AppleScript overhead |
| **Total** | **~200-480ms** | Depending on dictation length |

Typical 5-10 second dictation: **300-450ms**. Short 1-3 second dictation: **~200ms**.

WisprFlow comparison: 300-800ms (network-dependent). We beat them on short-to-medium dictations consistently, and match them on long ones.

**Design for consistency, not minimum latency.** Fixed model, fixed decoding strategy, model always hot = predictable performance. A tool that's always 350ms feels faster than one that's usually 200ms but occasionally 800ms.

### What This Design Explicitly Avoids

| Idea | Why it's excluded |
|------|-------------------|
| Streaming/incremental Whisper | Whisper is not a streaming model. Chunked inference degrades accuracy and creates stitching artifacts. |
| Adaptive model selection | Inconsistent UX. `small.en` INT4 is fast enough everywhere. |
| Regex filler removal | Fragile, context-dependent edge cases ("I like this"). Whisper handles fillers adequately. |
| LLM cleanup | Adds 500ms+ latency for negligible benefit given light cleanup needs. |
| Sequential conditioning | Error propagation. Each dictation is independent. |
| Always-on mic / wake word | Privacy violation. Contradicts core design priority. |
| Learning from corrections | Implicit data collection. Contradicts privacy priority. |
| Mel spectrogram pre-computation | ~15ms savings, significant threading complexity. Premature optimization. |
| VAD for audio trimming | Hold-to-talk already provides boundaries. VAD is for hallucination detection. |

### Implementation Phases

**Phase 1: Core Loop** — Get a working dictation tool. Use it for a week before adding anything.
1. Global hotkey (PyObjC CGEvent tap, hold-to-talk)
2. Audio capture (sounddevice, 16kHz mono, memory buffer)
3. Whisper inference (mlx-whisper, small.en INT4, model kept hot)
4. Text insertion (clipboard save → write → AppleScript paste → clipboard restore)
5. Menubar (rumps, recording state indicator)

**Phase 2: Reliability** — Make it trustworthy enough for daily use.
6. Hallucination guard (Silero VAD — discard output if no speech detected)
7. Cancel gesture (Escape during recording, or <300ms press = cancel)
8. Error handling (catch inference errors, show notification, never paste garbage)
9. Audio device selection (menubar dropdown, device change detection)
10. Permission handling (detect denied/revoked permissions, surface errors)
11. Warmup state (menubar shows "loading" until model is ready)

**Phase 3: Polish** — Add based on what actually annoys you during daily use.
12. Vocabulary replacement dict (post-processing string replacement)
13. Initial prompt (well-punctuated example for formatting bias)
14. Audio level indicator (pulsing menubar icon during recording)
15. Dictation history (plain text append, timestamp + transcript)
16. Insertion testing across apps (Safari, Chrome, VS Code, Terminal, Slack, etc.)

**Phase 4: Native Shell** (if warranted) — Replace rumps + PyObjC with a thin Swift .app wrapper if permission friction or menubar limitations become annoying.

### Dependencies

All free, all local:

| Dependency | Purpose |
|------------|---------|
| `mlx-whisper` | Whisper inference on Apple Silicon |
| `sounddevice` | Audio capture |
| `numpy` | Audio buffer management |
| `silero-vad` | Speech detection for hallucination guard (ONNX backend) |
| `onnxruntime` | Runtime for Silero VAD (avoids ~2GB PyTorch dependency) |
| `pyobjc` | CGEvent tap for global hotkey (MVP) |
| `rumps` | Menubar app (MVP, replaced by Swift in Phase 4) |

### Files

| File | Purpose |
|------|---------|
| `~/.config/dictation/vocab.txt` | User-maintained vocabulary replacements |
| `~/.config/dictation/config.toml` | Settings (hotkey, input device, model path) |
| `~/.config/dictation/history.txt` | Plain text dictation log |

---

## Open Questions

Things we need to answer during implementation. Not blockers — we have a plan for each — but the answers will affect the final experience.

### Q1: What is the actual inference latency on this specific machine?

Our latency budget assumes `small.en` INT4 via MLX does 150-400ms depending on audio length. These numbers come from community benchmarks on M2/M3 Pro chips, not from testing on this specific machine. MLX performance varies across chip generations and memory configurations.

If inference is slower than expected:
- Drop to `base.en` (faster, worse accuracy)
- Accept higher latency (may still beat WisprFlow)
- Investigate `whisper.cpp` Metal backend as an alternative to MLX

**When we'll know:** First run of Phase 1.

### Q2: Does `rumps` work reliably on the current macOS version?

`rumps` is effectively unmaintained. It works on most macOS versions but hasn't been tested against the latest releases. If it crashes or behaves oddly, the fallback is replacing it with `PyObjC` NSStatusBar directly (more code, same result) or accelerating Phase 4 (Swift shell).

**When we'll know:** First run of Phase 1.

### Q3: Will the CGEvent tap reliably detect Right Option hold/release?

Right Option generates `kCGEventFlagsChanged` events, not standard key events. Unknowns:
- Whether keycode 61 is consistent across keyboard layouts
- Whether any apps intercept Right Option before we see it
- Whether modifier flag detection works in all contexts (fullscreen, Spotlight)

If Right Option doesn't work, we switch to a different hotkey. The mechanism is sound — the specific key choice is uncertain.

**When we'll know:** Manual testing in Phase 1.

### Q4: Is the 150ms clipboard restore delay correct?

Too short: we restore before the paste completes (Electron apps are slow). Too long: user notices clipboard clobbering. The right number is probably app-dependent — native apps need ~50ms, Electron apps need ~200ms.

Start with 150ms, tune if it causes problems.

**When we'll know:** Integration testing across apps.

### Q5: Is `NSPasteboardTypeString` available in our pyobjc version?

We use `NSPasteboardTypeString` (modern) instead of the deprecated `NSStringPboardType`. If it fails at import, fall back to the string literal `"public.utf8-plain-text"`.

**When we'll know:** First test run.

---

## Things We're Unsure About

Deeper uncertainties where we've made a bet but aren't certain it's right.

### Whisper accuracy without any cleanup may not be good enough

We decided against regex filler removal and LLM cleanup, betting that Whisper's raw output is acceptable. But we don't actually know how often raw output for dictation-length clips will have filler words, wrong punctuation, repeated phrases, or garbled words from background noise.

If raw output requires too much manual correction, the tool fails the "daily driver" test even if it's fast. The escape hatch is a very small local LLM (Qwen 2.5 0.5B) for light cleanup — small enough to add ~50-100ms, not 500ms. But we're not building that until we know it's needed.

**Our bet:** Whisper `small.en` is accurate enough for dictation. Occasional corrections are less effort than maintaining a cleanup pipeline.

**Risk level:** Medium. Most likely reason the tool might feel worse than WisprFlow despite being faster.

### The vocabulary replacement approach is crude

Post-processing string replacement works but: it's case-sensitive (unless we add case-insensitive matching, risking false positives), can't handle context-dependent corrections, requires manually discovering each misrecognition, and could grow unwieldy.

**Our bet:** 10-30 replacements covers 90% of personal vocabulary issues. Fine for months of personal use.

### We don't know if hold-to-talk is the right interaction model

Hold-to-talk is physically uncomfortable for long dictations (30+ seconds), and accidental release truncates the transcription. Toggle mode avoids these issues but introduces others (forgetting to stop, accidental activations).

**Our bet:** Hold-to-talk is correct for short-to-medium dictation. If long dictation becomes common, add toggle mode as an option.

### `rumps` may be too limiting for the menubar UI we want

The spec describes pulsing audio level indicators, device selection dropdowns, and multiple states. `rumps` supports basic menubar apps but has no SF Symbols, limited dynamic menu updates, no audio visualization, and conflicts with other NSApplication run loops.

**Our bet:** `rumps` is fine for Phase 1. If we hit its limits in Phase 2/3, that triggers Phase 4 (Swift shell).

---

## Dream Scenarios

Performance and experience goals we aren't currently confident we can hit, and what would need to change.

### Dream 1: Sub-100ms perceived latency

**What it would feel like:** Text appears the instant you lift your finger. Indistinguishable from typing.

**Where we are:** 200-480ms depending on dictation length. Perceptually fast but not instant.

**What would need to change:** The bottleneck is Whisper inference. `tiny.en` gets us to ~50-130ms but accuracy drops noticeably. The real path is a smaller, dictation-specialized model — something distilled from Whisper for short-form English, quantized aggressively for Apple Silicon. Models like this exist in research but aren't packaged for easy use yet.

**Why we want it:** The difference between 300ms and 100ms is the difference between "fast tool" and "feels like typing." Sub-100ms is where dictation stops feeling like a separate mode and becomes a natural input method.

**Likelihood:** Low with current tools. Medium-high if distilled models become available through mlx-community.

### Dream 2: Streaming partial results

**What it would feel like:** Words appear while you're still speaking, like live subtitles. WisprFlow may already do this.

**Where we are:** Full audio processed on key release. Perceptual dead zone between stopping speech and text appearing.

**What would need to change:** Whisper is not a streaming model. Streaming requires either:
- A fundamentally different model architecture (CTC-based, or streaming Whisper variants like WhisperLive)
- VAD-based chunking with word-boundary stitching (complex, accuracy trade-off)
- Speculative display with revision (complex, flicker UX issues)

**Why we want it:** This is the single biggest UX differentiator. Even partial streaming — showing the first few words quickly — would make the tool feel dramatically more responsive. If WisprFlow does this, it's the one area where they'll always feel faster regardless of our absolute latency advantage.

**Likelihood:** Low in Phase 1-3. Possible as a Phase 5 exploration. For 1-10 second clips, 200-480ms delay after release is probably acceptable — streaming matters more for long-form dictation.

### Dream 3: Perfect accuracy on personal vocabulary without manual corrections

**What it would feel like:** "Kubernetes," "Asoona," "Tailscale" come out right every time without maintaining a replacement dictionary.

**Where we are:** Stock Whisper with a manually curated string replacement file.

**What would need to change:** Fine-tuning Whisper on a personal voice dataset. Record 50-100 utterances, LoRA fine-tune the decoder layers, run the personalized model. MLX supports this. The blocker is effort: recording training data, running the fine-tune, validating it doesn't degrade general accuracy.

A lighter alternative: CTC prefix-biasing to bias the decoder toward specific tokens. Supported by some Whisper implementations but not currently mlx-whisper.

**Why we want it:** The replacement dict works but creates friction — you have to discover each misrecognition, add it, and hope it doesn't create false positives elsewhere. A model that just knows your vocabulary eliminates an entire maintenance surface.

**Likelihood:** Medium, but not soon. Fine-tuning is a weekend project once the tool is stable. The replacement dict is good enough for months.

### Dream 4: Works perfectly in every macOS app, every time

**What it would feel like:** Never think about whether dictation will work in a particular app.

**Where we are:** Clipboard + Cmd+V works in most apps but has failure modes: Electron paste guards, apps that intercept Cmd+V, transient text fields (Spotlight), focus-change race conditions.

**What would need to change:** The "correct" solution is the macOS Accessibility API — directly inserting text into the focused element. But it's notoriously inconsistent across apps, especially Electron and custom text engines.

**Why we want it:** Every failed paste erodes trust. After 2-3 failures, you start second-guessing the tool and reaching for the keyboard instead. Reliability in the insertion path matters more than transcription accuracy — a perfect transcript that ends up in the wrong window is worse than an imperfect transcript in the right one.

**Likelihood:** Low for universal. High for the 10-15 apps actually used daily. The tail of edge cases is infinite but the head is small and testable.

### Dream 5: Zero perceived downtime — instant startup, no warmup

**What it would feel like:** Open laptop, immediately start dictating.

**Where we are:** 1-3 seconds model warmup at app launch. Dead zone if app crashes/restarts.

**What would need to change:** Keep the model memory-mapped via `launchd`, start inference process at login, keep it alive with crash recovery.

**Why we want it:** The 1-3 second warmup is fine for a once-per-session launch. But if the app crashes or macOS kills it for memory, the dead zone is confusing — the hotkey appears to work but nothing happens. A `launchd` keepalive eliminates this.

**Likelihood:** High and relatively easy — but Phase 3/4 concern.
