"""Test runner that runs each test in its own subprocess to avoid AppKit/audio conflicts."""

import subprocess
import sys
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(PROJECT_DIR, ".venv", "bin", "python3")

ALL_TESTS = [
    "test_normalization",
    "test_history",
    "test_clipboard",
    "test_audio",
    "test_transcription",
    "test_memory",
    "test_app_lifecycle",
    "test_postprocessing",
    "test_segmenter",
    "test_hotkey",
]

# Run a subset by passing module names, e.g.:
#   python tests/run_tests.py test_postprocessing test_history
# No args runs the full suite (the safe default for pre-ship checks).
TESTS = sys.argv[1:] if len(sys.argv) > 1 else ALL_TESTS

total_pass = 0
total_fail = 0
all_failures = []

print("Personal Dictation -- Stress Test Suite")
print("=" * 50)

for test_name in TESTS:
    result = subprocess.run(
        [PYTHON, "-c", f"""
import sys
sys.path.insert(0, '{PROJECT_DIR}')
sys.path.insert(0, '{os.path.join(PROJECT_DIR, "tests")}')
from stress_test import {test_name}, PASS, FAIL, ERRORS
import stress_test
{test_name}()
print(f'__RESULT__:{{stress_test.PASS}}:{{stress_test.FAIL}}')
for e in stress_test.ERRORS:
    print(f'__ERROR__:{{e}}')
"""],
        capture_output=True, text=True, timeout=120, cwd=PROJECT_DIR,
    )

    # Print output
    for line in result.stdout.strip().split("\n"):
        if line.startswith("__RESULT__:"):
            parts = line.split(":")
            p, f = int(parts[1]), int(parts[2])
            total_pass += p
            total_fail += f
        elif line.startswith("__ERROR__:"):
            all_failures.append(line[10:])
        else:
            print(line)

    if result.returncode not in (0, None):
        if result.returncode == -11:
            print(f"  FAIL  {test_name} -- SEGFAULT")
            total_fail += 1
            all_failures.append(f"{test_name}: SEGFAULT")
        elif result.returncode != 0 and "RESULT" not in result.stdout:
            stderr_short = result.stderr.strip().split("\n")[-1] if result.stderr else "unknown"
            print(f"  FAIL  {test_name} -- exit code {result.returncode}: {stderr_short}")
            total_fail += 1
            all_failures.append(f"{test_name}: exit {result.returncode}")

print("\n" + "=" * 50)
print(f"Results: {total_pass} passed, {total_fail} failed")
if all_failures:
    print("\nFailures:")
    for e in all_failures:
        print(f"  - {e}")
else:
    print("\nAll tests passed!")

sys.exit(1 if total_fail > 0 else 0)
