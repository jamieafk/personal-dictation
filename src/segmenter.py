"""Streaming segmenter: transcribe-while-you-hold.

While the hotkey is held, a poll thread watches the growing recording and closes
a speech segment at a natural pause (or a hard length cap), handing each closed
segment to a single-worker transcription queue. By release, everything but the
final tail is usually already transcribed, so release-to-text latency stops
scaling with how long the user spoke.

Design notes:
- Single worker (serial GPU on M3 — concurrent transcribes would contend).
- Segment boundaries are exact cumulative native-sample indices, so consecutive
  extracts reconstruct the recording with no gaps/overlaps.
- Each segment transcribes independently (self-normalized, own VAD guard), so a
  silent segment simply contributes "" and the per-segment hallucination guard is
  preserved. condition_on_previous_text is already False, so no cross-segment
  context means no cross-segment repetition loops.
- audio source + transcribe fn are injected, so the logic is unit-testable with
  fakes (no audio hardware / model needed).
- Short dictations never reach MIN_SEG_S, so they close as a single tail segment
  on release == today's batch behavior.
- Speculative tail: at a pause too short to close a segment, the idle worker
  pre-transcribes the pending audio. If nothing is said after it (quiet by peak
  AND no VAD speech), the segment reuses that text instead of decoding after
  release. Anything after the speculation that might be speech → normal decode.
"""

import logging
import queue
import threading
from collections import namedtuple
import numpy as np

from src import config

log = logging.getLogger("dictation")

_WORKER_JOIN_TIMEOUT_S = 30.0  # generous: a queued tail could be a full ~24s segment
_POLL_JOIN_TIMEOUT_S = 2.0

_SEG = "seg"
_SPEC = "spec"
# Finished speculation of native [start, end); floor = median 32ms-frame RMS of the
# pause that triggered it (the room's noise level, for the reuse energy check).
_Spec = namedtuple("_Spec", "start end text floor")
_RATE = 16000   # transcribe_fn input rate
_FRAME = 512    # 32ms energy frames


def _frame_rms(audio):
    """RMS per 32ms frame; a trailing partial frame counts if it's at least 8ms."""
    k, r = divmod(len(audio), _FRAME)
    rms = list(np.sqrt(np.mean(audio[:k * _FRAME].reshape(k, _FRAME) ** 2, axis=1))) if k else []
    if r and (r >= _FRAME // 4 or not k):
        rms.append(float(np.sqrt(np.mean(audio[k * _FRAME:] ** 2))))
    return np.asarray(rms, dtype=np.float64)


def _energy_why(ratios, loud):
    """Miss reason for an energy veto: max ratio, frames over the threshold, and
    the longest consecutive run — words sustain 4+ frames, clicks/flicker 1–2."""
    run = best = 0
    for x in loud:
        run = run + 1 if x else 0
        best = max(best, run)
    return f"energy={float(ratios.max()):.2f},frames={int(loud.sum())},run={best}"


class StreamingSegmenter:
    def __init__(self, audio_src, transcribe_fn, *, speech_fn=None, poll_s=None, min_seg_s=None,
                 max_seg_s=None, silence_peak=None, silence_win_s=None, spec_win_s=None,
                 floor_ratio=None):
        # audio_src: samples_captured(), recent_peak(w), peak_between(s,e), extract_16k(s,e), native_rate()
        self._audio = audio_src
        self._transcribe = transcribe_fn  # transcribe_fn(audio_16k) -> str
        # speech_fn(audio_16k) -> bool enables speculative tails; with the peak and
        # energy checks it vetoes reusing a speculation if anything followed it.
        self._speech_fn = speech_fn
        self._poll_s = poll_s if poll_s is not None else config.STREAM_POLL_S
        self._min_seg_s = min_seg_s if min_seg_s is not None else config.STREAM_MIN_SEG_S
        self._max_seg_s = max_seg_s if max_seg_s is not None else config.STREAM_MAX_SEG_S
        self._silence_peak = silence_peak if silence_peak is not None else config.STREAM_SILENCE_PEAK
        self._silence_win_s = silence_win_s if silence_win_s is not None else config.STREAM_SILENCE_WIN_S
        self._spec_win_s = spec_win_s if spec_win_s is not None else config.STREAM_SPEC_SILENCE_S
        self._floor_ratio = floor_ratio if floor_ratio is not None else config.STREAM_SPEC_FLOOR_RATIO

        self._lock = threading.Lock()
        self._retired = None  # a cancelled session's worker; the next worker joins it first
        self._reset_state()

    def _reset_state(self):
        self._seg_index = 0
        self._seg_start_native = 0
        self._results = {}        # idx -> transcribed text
        self._seg_audio = {}      # idx -> 16k audio (for retry/fallback reconstruction)
        self._spec = {"latest": None, "runs": 0, "hit_idx": set(), "why": {}}
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._poll = None
        self._worker = None

    # --- lifecycle ---

    def start(self, prime=None):
        """Begin segmenting. Call on hotkey press, after recording has started.
        prime: optional callable run first on the worker (serial with segment
        transcribes), e.g. a model re-warm that overlaps with the user speaking."""
        with self._lock:
            prev, self._retired = self._retired, None
            self._reset_state()
            self._worker = threading.Thread(
                target=self._worker_loop, args=(self._queue, self._results, self._spec, prime, prev),
                daemon=True)
            self._worker.start()
            self._poll = threading.Thread(target=self._poll_loop, args=(self._stop,), daemon=True)
            self._poll.start()

    def stop_polling(self):
        """Stop auto-closing segments. Call on release/cancel before close_tail.
        Wakes the poll thread rather than waiting out its sleep — that wait used to
        add up to a full poll interval to every release."""
        self._stop.set()
        poll = self._poll
        if poll is not None and poll is not threading.current_thread():
            poll.join(timeout=_POLL_JOIN_TIMEOUT_S)
        self._poll = None

    def close_tail(self, end_native):
        """Enqueue the final [seg_start, end_native) tail as the last segment.
        Call once after stop_polling()."""
        self._close_segment(end_native)

    def finalize(self):
        """Wait for all queued segments to transcribe, then return the assembled
        text in segment order (empties dropped). Call on the processing thread."""
        self._queue.join()                 # all enqueued segments transcribed
        self._queue.put(None)              # stop the worker
        worker = self._worker
        if worker is not None:
            worker.join(timeout=_WORKER_JOIN_TIMEOUT_S)
        return self._assemble()

    def _assemble(self):
        """Join segment texts in index order, dropping empties (silent/rejected)."""
        with self._lock:
            n = self._seg_index
            parts = [self._results.get(i, "") for i in range(n)]
        return " ".join(p for p in parts if p).strip()

    def cancel(self):
        """Discard everything and stop threads. Call on Escape-cancel / quick tap
        (main thread), so it never waits on a decode: the worker is retired and the
        next session's worker joins it before decoding, keeping the GPU serial."""
        self.stop_polling()
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass
        self._retire()

    def reset(self):
        """Drop the finished session's results and retained audio. Call after
        finalize, once anything kept for retry has been copied out, and before the
        app returns to IDLE (a new press must not race this)."""
        self._retire()

    def _retire(self):
        self._queue.put(None)  # releases a worker still waiting (no-op after finalize)
        with self._lock:
            worker = self._worker
            self._reset_state()
            if worker is not None and worker.is_alive() and worker is not threading.current_thread():
                self._retired = worker

    def spec_stats(self):
        """(tail outcome 'hit'|'miss(<why>)'|'none', speculative decodes run) for
        the finished session — logged so speculation can be tuned from real use."""
        with self._lock:
            runs = self._spec["runs"]
            tail = self._seg_index - 1
            hit = tail in self._spec["hit_idx"]
            why = self._spec["why"].get(tail, "stale")
        return ("hit" if hit else f"miss({why})" if runs else "none"), runs

    def full_audio(self):
        """Concatenated 16k audio of all segments, in order — for retry/fallback."""
        with self._lock:
            segs = [self._seg_audio[i] for i in range(self._seg_index) if i in self._seg_audio]
        if not segs:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(segs)

    @property
    def seg_start_native(self):
        return self._seg_start_native

    # --- internals ---

    def _poll_loop(self, stop):
        while not stop.wait(self._poll_s):
            try:
                now = self._audio.samples_captured()
                if self._maybe_close(now) is None:
                    self._maybe_speculate(now)
            except Exception:
                log.exception("Segmenter poll error")  # never let the poll thread die

    def _maybe_close(self, now_native):
        """Decide whether to close a segment at now_native. Returns the reason
        string if it closed, else None. Pure given the injected audio source."""
        rate = max(1, self._audio.native_rate())
        unseg_s = (now_native - self._seg_start_native) / rate
        if unseg_s >= self._max_seg_s:
            self._close_segment(now_native)
            return "max"
        if unseg_s >= self._min_seg_s:
            if self._audio.recent_peak(self._silence_win_s) < self._silence_peak:
                self._close_segment(now_native)
                return "pause"
        return None

    def _maybe_speculate(self, now_native):
        """At a pause too short to close a segment, queue a speculative transcribe
        of the pending audio on the idle worker. Returns "spec" if queued, else None."""
        if self._speech_fn is None:
            return None
        rate = max(1, self._audio.native_rate())
        with self._lock:
            start = self._seg_start_native
            latest = self._spec["latest"]
        pending = now_native - start
        if pending <= int(self._spec_win_s * rate):
            return None                    # nothing before the pause yet
        if pending >= int(self._min_seg_s * rate):
            return None                    # long pending audio: costly to re-decode, and a
                                           # pause now closes a real segment anyway
        if self._queue.unfinished_tasks:   # worker busy, or a speculation already pending
            return None
        if self._audio.recent_peak(self._spec_win_s) >= self._silence_peak:
            return None                    # still speaking
        if (latest is not None and latest.start == start
                and self._audio.peak_between(latest.end, now_native) < self._silence_peak):
            return None                    # nothing said since the last speculation
        self._queue.put((_SPEC, None, start, now_native, self._audio.extract_16k(start, now_native)))
        return "spec"

    def _reuse_spec(self, spec, idx, start, end, audio):
        """The speculative text for segment [start, end) if a speculation covered
        its start and everything after it is silent, else None. Silent = under the
        absolute pause peak AND no 32ms frame louder than the pause's own noise floor
        × floor_ratio AND speech_fn finds nothing. Wrongly reusing drops words, a
        miss only costs latency, so every check errs toward a miss. Runs on the
        worker, so the VAD model is never used concurrently."""
        with self._lock:
            latest = spec["latest"]
        if latest is None or latest.start != start or not start < latest.end <= end:
            return None
        rest = audio[round(len(audio) * (latest.end - start) / (end - start)):]
        if len(rest):
            ratios = _frame_rms(rest) / max(latest.floor, 1e-4)
            loud = ratios > self._floor_ratio
            why = ("peak" if float(np.max(np.abs(rest))) >= self._silence_peak
                   else _energy_why(ratios, loud) if loud.any()
                   else "vad" if self._speech_fn(rest) else None)
            if why:
                with self._lock:
                    spec["why"][idx] = why
                return None
        return latest.text

    def _close_segment(self, end_native):
        with self._lock:
            start = self._seg_start_native
            if end_native <= start:
                return  # nothing new captured
            idx = self._seg_index
            self._seg_index += 1
            self._seg_start_native = end_native
        seg_audio = self._audio.extract_16k(start, end_native)  # outside lock (concat/resample)
        with self._lock:
            self._seg_audio[idx] = seg_audio
        self._queue.put((_SEG, idx, start, end_native, seg_audio))
        log.debug("Segment %d closed (%d samples)", idx, len(seg_audio))

    def _worker_loop(self, q, results, spec, prime, prev):
        # q/results/spec are this session's objects, bound at start: a cancelled
        # session's worker may still be finishing a decode, and must not consume the
        # next session's queue or write into its results.
        if prev is not None:
            prev.join(timeout=_WORKER_JOIN_TIMEOUT_S)  # one decode on the GPU at a time
        if prime is not None:
            try:
                prime()
            except Exception:
                log.exception("Segmenter prime failed")
        while True:
            item = q.get()
            if item is None:
                q.task_done()
                break
            kind, idx, start, end, seg_audio = item
            if kind == _SPEC:
                try:
                    text = self._transcribe(seg_audio)
                    pause = seg_audio[-int(self._spec_win_s * _RATE):]
                    floor = float(np.median(_frame_rms(pause))) if len(pause) else 0.0
                    with self._lock:
                        spec["latest"] = _Spec(start, end, text, floor)
                        spec["runs"] += 1
                except Exception:
                    log.exception("Speculative transcription failed")
                q.task_done()
                continue
            try:
                text = self._reuse_spec(spec, idx, start, end, seg_audio)
            except Exception:
                log.exception("Speculation check failed; decoding segment %d normally", idx)
                with self._lock:
                    spec["why"][idx] = "error"
                text = None
            try:
                if text is not None:
                    with self._lock:
                        spec["hit_idx"].add(idx)
                else:
                    text = self._transcribe(seg_audio)
            except Exception:
                log.exception("Segment %d transcription failed", idx)
                text = ""
            with self._lock:
                results[idx] = text
            q.task_done()
