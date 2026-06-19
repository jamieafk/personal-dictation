"""Stress tests for Personal Dictation. Run with: python tests/stress_test.py"""

import os
import sys
import time
import signal
import subprocess
import tempfile
import shutil
import resource

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

PASS = 0
FAIL = 0
ERRORS = []


def report(name, passed, detail=""):
    global PASS, FAIL
    if passed:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  FAIL  {name} -- {detail}")


# ============================================================
# Test 1: Normalization edge cases
# ============================================================
def test_normalization():
    print("\n=== Test 1: Normalization Edge Cases ===")
    from src.transcribe import _normalize

    # Empty array
    empty = np.array([], dtype=np.float32)
    result = _normalize(empty)
    report("empty array handled", len(result) == 0)

    # Absolute silence
    silence = np.zeros(16000, dtype=np.float32)
    result = _normalize(silence, precomputed_peak=0.0)
    report("absolute silence unchanged", np.array_equal(result, silence))

    # Near silence (peak < 0.001)
    near_silence = np.ones(16000, dtype=np.float32) * 0.0005
    result = _normalize(near_silence, precomputed_peak=0.0005)
    report("near silence not boosted", np.array_equal(result, near_silence))

    # Quiet (peak = 0.05) — should boost to target 0.5
    quiet = np.ones(16000, dtype=np.float32) * 0.05
    result = _normalize(quiet, precomputed_peak=0.05)
    report("quiet audio boosted", abs(result[0] - 0.5) < 0.001, f"got {result[0]}")

    # At threshold (0.1) — should NOT boost
    threshold = np.ones(16000, dtype=np.float32) * 0.1
    result = _normalize(threshold, precomputed_peak=0.1)
    report("at threshold not boosted", np.array_equal(result, threshold))

    # Normal volume
    normal = np.ones(16000, dtype=np.float32) * 0.5
    result = _normalize(normal, precomputed_peak=0.5)
    report("normal volume unchanged", np.array_equal(result, normal))

    # Gain capped at 100x (peak = 0.002)
    tiny = np.ones(16000, dtype=np.float32) * 0.002
    result = _normalize(tiny, precomputed_peak=0.002)
    report("gain capped at 100x", abs(result[0] - 0.2) < 0.001, f"got {result[0]}")


# ============================================================
# Test 2: History persistence
# ============================================================
def test_history():
    print("\n=== Test 2: History Persistence ===")
    from src import history

    original_path = history.HISTORY_PATH
    tmp_dir = tempfile.mkdtemp()
    history.HISTORY_PATH = os.path.join(tmp_dir, "test_history.txt")
    history._cached_word_count = None

    try:
        # Rapid appends
        for i in range(100):
            history.append("TestApp", f"Test entry number {i}")
        entries = history.load_all()
        report("100 rapid appends", len(entries) == 100, f"got {len(entries)}")

        # Word count cache
        wc = history.total_word_count()
        expected = sum(e.word_count for e in entries)
        report("word count matches", wc == expected, f"cached={wc}, actual={expected}")

        # Cache increments
        history.append("TestApp", "one two three four five")
        wc2 = history.total_word_count()
        report("cache increments", wc2 == wc + 5, f"was {wc}, now {wc2}")

        # Pipe characters
        history.append("TestApp", "pipe | test | here")
        entries2 = history.load_all()
        report("pipe chars preserved", entries2[-1].text == "pipe | test | here",
               f"got: {repr(entries2[-1].text)}")

        # Unicode
        history.append("TestApp", "caf\u00e9 na\u00efve r\u00e9sum\u00e9")
        entries3 = history.load_all()
        report("unicode preserved", "caf\u00e9" in entries3[-1].text)

        # Long text (10KB)
        long_text = "word " * 2000
        history.append("TestApp", long_text.strip())
        entries4 = history.load_all()
        report("10KB entry preserved", len(entries4[-1].text) > 9000)

        # Corrupt file
        with open(history.HISTORY_PATH, "a") as f:
            f.write("garbage\n\nbroken | line\n")
        entries5 = history.load_all()
        report("corrupt lines skipped", len(entries5) == len(entries4),
               f"expected {len(entries4)}, got {len(entries5)}")

    finally:
        history.HISTORY_PATH = original_path
        history._cached_word_count = None
        shutil.rmtree(tmp_dir)


# ============================================================
# Test 3: Clipboard round-trip
# ============================================================
def test_clipboard():
    print("\n=== Test 3: Clipboard Round-Trip ===")
    from src.paste import save_clipboard, set_clipboard_text, restore_clipboard
    from AppKit import NSPasteboard

    original = save_clipboard()

    try:
        # Set and read
        set_clipboard_text("stress test 123")
        pb = NSPasteboard.generalPasteboard()
        got = pb.stringForType_("public.utf8-plain-text")
        report("set/read text", got == "stress test 123", f"got: {repr(got)}")

        # Restore
        restore_clipboard(original)
        after = save_clipboard()
        report("clipboard restored", len(after) == len(original))

        # Empty clipboard
        pb.clearContents()
        empty = save_clipboard()
        report("empty clipboard save", empty == [])
        set_clipboard_text("temp")
        restore_clipboard(empty)
        report("empty clipboard restore", True)

        # Rapid 10x
        for i in range(10):
            s = save_clipboard()
            set_clipboard_text(f"rapid {i}")
            restore_clipboard(s)
        report("10x rapid operations", True)

        # Large (100KB)
        set_clipboard_text("x" * 100_000)
        large = save_clipboard()
        report("100KB save", len(large) > 0)

    finally:
        restore_clipboard(original)


# ============================================================
# Test 4: Audio capture
# ============================================================
def test_audio():
    print("\n=== Test 4: Audio Capture ===")
    from src import audio

    # Stop without start
    report("stop without start", audio.stop_recording() is None)

    # Double stop
    report("double stop", audio.stop_recording() is None)

    # Rapid start/stop (20x)
    for i in range(20):
        audio.start_recording()
        time.sleep(0.05)
        audio.stop_recording()
    report("20x rapid start/stop", True)

    # 1s recording
    audio.start_recording()
    time.sleep(1.0)
    buf = audio.stop_recording()
    report("1s recording returns data",
           buf is not None and len(buf) > 10000,
           f"got {len(buf) if buf is not None else 'None'}")

    # RMS reset
    report("RMS reset after stop", audio.get_rms_level() == 0.0)

    # Double start
    audio.start_recording()
    audio.start_recording()
    time.sleep(0.5)
    audio.stop_recording()
    report("double start no crash", True)


# ============================================================
# Test 5: Transcription stress
# ============================================================
def test_transcription():
    print("\n=== Test 5: Transcription Stress ===")
    from src.transcribe import transcribe, warmup

    t0 = time.monotonic()
    warmup()
    report("warmup completes", time.monotonic() - t0 < 30)

    # Empty array
    try:
        transcribe(np.array([], dtype=np.float32))
        report("empty array no crash", True)
    except Exception as e:
        report("empty array no crash", False, str(e))

    # Silence
    report("silence returns empty", transcribe(np.zeros(16000, dtype=np.float32)) == "")

    # Noise
    report("noise returns empty",
           transcribe(np.random.randn(48000).astype(np.float32) * 0.01) == "")

    # 20 rapid transcriptions
    timings = []
    for i in range(20):
        buf = np.random.randn(48000).astype(np.float32) * 0.01
        t0 = time.monotonic()
        transcribe(buf)
        timings.append(time.monotonic() - t0)
    avg = sum(timings) / len(timings)
    report(f"20x transcriptions avg={avg*1000:.0f}ms", max(timings) < 2.0)


# ============================================================
# Test 6: Memory leak detection
# ============================================================
def test_memory():
    print("\n=== Test 6: Memory Leak Detection ===")
    from src.transcribe import transcribe, warmup

    # Warmup first so model loading doesn't count as "growth"
    warmup()
    # Run a few transcriptions to stabilize
    for _ in range(3):
        transcribe(np.random.randn(16000).astype(np.float32) * 0.01)

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)

    for i in range(20):
        size = 16000 * (1 + (i % 5))
        buf = np.random.randn(size).astype(np.float32) * 0.01
        transcribe(buf)

    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    growth = rss_after - rss_before
    report(f"memory growth {growth:.1f}MB over 20 transcriptions", growth < 50)


# ============================================================
# Test 7: App lifecycle
# ============================================================
def test_app_lifecycle():
    print("\n=== Test 7: App Lifecycle ===")
    import fcntl

    from src import config
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python = os.path.join(project_dir, ".venv", "bin", "python3")
    lock_path = os.path.join(config.CONFIG_DIR, "dictation.lock")

    subprocess.run(["pkill", "-f", "python.*-m src"], capture_output=True)
    subprocess.run(["pkill", "-f", "Personal Dictation"], capture_output=True)
    time.sleep(2)

    # Start
    proc = subprocess.Popen([python, "-m", "src"], cwd=project_dir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    report("app starts", proc.poll() is None, "exited early")

    if proc.poll() is None:
        # Stop
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        report("app stops", proc.returncode is not None)

        time.sleep(1)

        # Lock released
        try:
            lf = open(lock_path, "w")
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
            report("lock released", True)
        except OSError:
            report("lock released", False, "still held")

        # Restart
        proc2 = subprocess.Popen([python, "-m", "src"], cwd=project_dir,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5)
        report("restart works", proc2.poll() is None)

        if proc2.poll() is None:
            # Single-instance guard
            proc3 = subprocess.Popen([python, "-m", "src"], cwd=project_dir,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                proc3.wait(timeout=5)
                report("single-instance guard", proc3.returncode == 0)
            except subprocess.TimeoutExpired:
                report("single-instance guard", False, "hung")
                proc3.kill()

            proc2.terminate()
            try:
                proc2.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc2.kill()


# ============================================================
# Test 8: Post-processing (vocab, fillers, stutters)
# ============================================================
def test_postprocessing():
    print("\n=== Test 8: Post-Processing ===")
    from src.postprocess import (
        apply_vocab, remove_fillers, collapse_stutters, clean,
        load_vocab, _soundex,
    )

    # --- Soundex ---
    report("soundex basic", _soundex("Robert") == "R163")
    report("soundex similar", _soundex("Robert") == _soundex("Rupert"))

    # --- Vocab: exact match (case-insensitive) ---
    vocab = ["Karpathy", "ChatGPT", "ChargeBee", "Kubernetes", "MacBook Pro"]
    report("exact lowercase", apply_vocab("karpathy", vocab) == "Karpathy")
    report("exact mixed case", apply_vocab("chatgpt", vocab) == "ChatGPT")

    # --- Vocab: fuzzy match ---
    report("fuzzy 1-word", apply_vocab("carpathy", vocab) == "Karpathy")
    report("fuzzy 2-word ngram",
           apply_vocab("car pathy is great", vocab) == "Karpathy is great")
    report("fuzzy charge bee",
           apply_vocab("charge bee", vocab) == "ChargeBee")

    # --- Vocab: case preservation ---
    report("case ALLCAPS",
           apply_vocab("KARPATHY", vocab) == "KARPATHY")

    # --- Vocab: punctuation preservation ---
    result = apply_vocab("car pathy, is great", vocab)
    report("punctuation preserved",
           result == "Karpathy, is great",
           f"got: {repr(result)}")

    # --- Vocab: no false positives ---
    report("no false positive",
           apply_vocab("the weather is nice today", vocab) == "the weather is nice today")

    # --- Vocab: empty ---
    report("empty vocab passthrough",
           apply_vocab("hello world", []) == "hello world")

    # --- Filler removal ---
    report("basic fillers",
           remove_fillers("um I think uh it works") == "I think it works")
    report("filler with comma",
           remove_fillers("Um, the thing").strip() in ("The thing", "the thing"))
    report("no fillers unchanged",
           remove_fillers("this is fine") == "this is fine")
    report("all fillers",
           remove_fillers("um uh hmm").strip() == "")
    report("filler mid-sentence",
           remove_fillers("I think um that works") == "I think that works")

    # --- Stutter collapse ---
    report("stutter 4x", collapse_stutters("I I I I think") == "I think")
    report("stutter 3x", collapse_stutters("so so so what") == "so what")
    report("stutter preserved 2x", collapse_stutters("no no") == "no no")
    report("no stutter", collapse_stutters("hello world") == "hello world")
    report("long word no collapse",
           collapse_stutters("the the the end") == "the the the end")

    # --- Combined pipeline ---
    result = clean("um I I I I think carpathy is uh great", vocab)
    report("full pipeline",
           result == "I think Karpathy is great",
           f"got: {repr(result)}")

    # --- Edge cases ---
    report("empty string", clean("", vocab) == "")
    report("whitespace only", clean("   ", vocab) == "")
    report("None vocab", clean("hello", None) == "hello")

    # --- load_vocab missing file ---
    report("missing vocab file", load_vocab("/nonexistent/path.txt") == [])

    # --- load_vocab with temp file ---
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    try:
        tmp.write("# comment\nKarpathy\n\nChatGPT\n")
        tmp.close()
        loaded = load_vocab(tmp.name)
        report("load_vocab parses", loaded == ["Karpathy", "ChatGPT"],
               f"got: {repr(loaded)}")
    finally:
        os.unlink(tmp.name)


# ============================================================
# Test 9: Streaming segmenter
# ============================================================
def test_segmenter():
    print("\n=== Test 9: Streaming Segmenter ===")
    import time as _t
    from src.segmenter import StreamingSegmenter

    class FakeAudio:
        def __init__(self, rate=16000):
            self._rate = rate; self.samples = 0; self.peak = 1.0; self.buffer = None
        def native_rate(self): return self._rate
        def samples_captured(self): return self.samples
        def recent_peak(self, w): return self.peak
        def extract_16k(self, s, e):
            if self.buffer is not None:
                return self.buffer[s:e].astype(np.float32)
            return np.zeros(max(0, e - s), dtype=np.float32)

    # --- decision logic ---
    fa = FakeAudio(rate=16000)
    seg = StreamingSegmenter(fa, lambda a: "x", min_seg_s=2.0, max_seg_s=10.0,
                             silence_peak=0.5, silence_win_s=0.1)
    fa.samples = int(1.0 * 16000); fa.peak = 0.0
    report("no close before MIN_SEG_S",
           seg._maybe_close(fa.samples) is None and seg._seg_index == 0)
    fa.samples = int(3.0 * 16000); fa.peak = 0.0
    report("close at pause after MIN_SEG_S",
           seg._maybe_close(fa.samples) == "pause" and seg._seg_index == 1
           and seg.seg_start_native == fa.samples)
    fa.samples = int(6.0 * 16000); fa.peak = 1.0
    report("no close while still speaking",
           seg._maybe_close(fa.samples) is None and seg._seg_index == 1)
    fa.samples = int(13.0 * 16000); fa.peak = 1.0  # unseg 10s >= max, ignores speech
    report("force close at MAX_SEG_S",
           seg._maybe_close(fa.samples) == "max" and seg._seg_index == 2)
    before = seg._seg_index
    seg._close_segment(seg.seg_start_native)  # end == start
    report("no-op close when nothing new", seg._seg_index == before)

    # --- assembly: index order, empties dropped ---
    seg2 = StreamingSegmenter(FakeAudio(), lambda a: "x")
    with seg2._lock:
        seg2._seg_index = 4
        seg2._results = {0: "Hello there", 1: "", 2: "general", 3: "Kenobi"}
    report("assemble in index order, empties dropped",
           seg2._assemble() == "Hello there general Kenobi", repr(seg2._assemble()))

    # --- full_audio reconstructs with no gaps/overlaps ---
    fa2 = FakeAudio(rate=16000); fa2.buffer = np.arange(48000, dtype=np.float32)
    seg3 = StreamingSegmenter(fa2, lambda a: "x")
    seg3._close_segment(16000); seg3._close_segment(32000); seg3._close_segment(48000)
    full = seg3.full_audio()
    report("full_audio reconstructs recording exactly",
           len(full) == 48000 and np.array_equal(full, fa2.buffer), f"len={len(full)}")

    # --- threaded integration: streaming closes segments during the 'hold' ---
    class TimelineAudio:
        def __init__(self, rate=16000, pauses=()):
            self._rate = rate; self._t0 = _t.monotonic(); self._pauses = pauses; self._frozen = None
        def _elapsed(self):
            return (self._frozen if self._frozen is not None else _t.monotonic()) - self._t0
        def native_rate(self): return self._rate
        def samples_captured(self): return int(self._elapsed() * self._rate)
        def recent_peak(self, w):
            t = self._elapsed()
            return 0.0 if any(a <= t <= b for (a, b) in self._pauses) else 1.0
        def extract_16k(self, s, e): return np.zeros(max(0, e - s), dtype=np.float32)
        def stop(self): self._frozen = _t.monotonic()

    calls = {"n": 0}
    def fake_tx(audio):
        calls["n"] += 1
        return "word"
    ta = TimelineAudio(rate=16000, pauses=[(0.25, 0.45), (0.7, 0.9)])
    segS = StreamingSegmenter(ta, fake_tx, poll_s=0.02, min_seg_s=0.2, max_seg_s=5.0,
                              silence_peak=0.5, silence_win_s=0.05)
    segS.start()
    _t.sleep(1.1)
    segS.stop_polling(); ta.stop()
    mid = segS._seg_index
    segS.close_tail(ta.samples_captured())
    text = segS.finalize()
    report("streaming closed segment(s) mid-hold", mid >= 1, f"mid={mid}")
    report("every segment transcribed and assembled",
           len(text.split()) == segS._seg_index and calls["n"] == segS._seg_index,
           f"words={len(text.split())} idx={segS._seg_index} calls={calls['n']}")

    # --- cancel discards everything ---
    segC = StreamingSegmenter(TimelineAudio(rate=16000), fake_tx, poll_s=0.02, min_seg_s=0.2)
    segC.start(); _t.sleep(0.1); segC.cancel()
    report("cancel resets state", segC._seg_index == 0 and segC._assemble() == "")


# ============================================================
# Run all tests
# ============================================================
if __name__ == "__main__":
    print("Personal Dictation -- Stress Test Suite")
    print("=" * 50)

    test_normalization()
    test_history()
    test_clipboard()
    test_audio()
    test_transcription()
    test_memory()
    test_app_lifecycle()
    test_postprocessing()
    test_segmenter()

    print("\n" + "=" * 50)
    print(f"Results: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  - {e}")
    else:
        print("\nAll tests passed!")

    sys.exit(1 if FAIL > 0 else 0)
