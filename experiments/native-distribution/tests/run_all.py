"""Run every experiment test; fail on any failure, error or skip, and when
nothing ran. In CI a skipped verification is not a pass.

    python3 -I -B experiments/native-distribution/tests/run_all.py
"""

import os
import sys
import unittest

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(here))
skipped = [f"{test.id()}: {reason}" for test, reason in result.skipped]
for line in skipped:
    print(f"run_all: skipped, which counts as a failure here: {line}", file=sys.stderr)
print(f"run_all: {result.testsRun} tests ran, {len(skipped)} skipped", file=sys.stderr)
sys.exit(0 if result.wasSuccessful() and not skipped and result.testsRun > 0 else 1)
