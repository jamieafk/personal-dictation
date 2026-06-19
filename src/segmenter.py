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
  on release == today's batch behavior (no regression, no benefit — by design).
"""

import logging
import queue
import threading
import numpy as np

from src import config

log = logging.getLogger("dictation")

_WORKER_JOIN_TIMEOUT_S = 30.0  # generous: a queued tail could be a full ~24s segment
_POLL_JOIN_TIMEOUT_S = 2.0


class StreamingSegmenter:
    def __init__(self, audio_src, transcribe_fn, *, poll_s=None, min_seg_s=None,
                 max_seg_s=None, silence_peak=None, silence_win_s=None):
        self._audio = audio_src          # samples_captured(), recent_peak(w), extract_16k(s,e), native_rate()
        self._transcribe = transcribe_fn  # transcribe_fn(audio_16k) -> str
        self._poll_s = poll_s if poll_s is not None else config.STREAM_POLL_S
        self._min_seg_s = min_seg_s if min_seg_s is not None else config.STREAM_MIN_SEG_S
        self._max_seg_s = max_seg_s if max_seg_s is not None else config.STREAM_MAX_SEG_S
        self._silence_peak = silence_peak if silence_peak is not None else config.STREAM_SILENCE_PEAK
        self._silence_win_s = silence_win_s if silence_win_s is not None else config.STREAM_SILENCE_WIN_S

        self._lock = threading.Lock()
        self._reset_state()

    def _reset_state(self):
        self._seg_index = 0
        self._seg_start_native = 0
        self._results = {}        # idx -> transcribed text
        self._seg_audio = {}      # idx -> 16k audio (for retry/fallback reconstruction)
        self._queue = queue.Queue()
        self._running = False
        self._poll = None
        self._worker = None

    # --- lifecycle ---

    def start(self):
        """Begin segmenting. Call on hotkey press, after recording has started."""
        with self._lock:
            self._reset_state()
            self._running = True
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()
            self._poll = threading.Thread(target=self._poll_loop, daemon=True)
            self._poll.start()

    def stop_polling(self):
        """Stop auto-closing segments. Call on release/cancel before close_tail."""
        self._running = False
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
        """Discard everything and stop threads. Call on Escape-cancel."""
        self.stop_polling()
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass
        self._queue.put(None)
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=_POLL_JOIN_TIMEOUT_S)
        with self._lock:
            self._reset_state()

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

    def _poll_loop(self):
        import time
        while self._running:
            time.sleep(self._poll_s)
            if not self._running:
                break
            try:
                self._maybe_close(self._audio.samples_captured())
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
        self._queue.put((idx, seg_audio))
        log.debug("Segment %d closed (%d samples)", idx, len(seg_audio))

    def _worker_loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            idx, seg_audio = item
            try:
                text = self._transcribe(seg_audio)
            except Exception:
                log.exception("Segment %d transcription failed", idx)
                text = ""
            with self._lock:
                self._results[idx] = text
            self._queue.task_done()
