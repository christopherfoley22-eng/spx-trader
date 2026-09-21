"""The runner must report script failures and exclude broker-connected names."""

import tempfile
from pathlib import Path
import offline_test_runner as runner

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    for name in runner.IBKR_CONNECTED:
        (root / name).write_text("raise RuntimeError('MUST NOT RUN')\n")
    (root / "passing_test.py").write_text("assert True\n")
    failing = root / "failing_test.py"
    failing.write_text("raise AssertionError('EXPECTED FAILURE')\n")
    original = runner.ROOT
    try:
        runner.ROOT = root
        assert runner.main() == 1
        failing.write_text("assert True\n")
        assert runner.main() == 0
    finally:
        runner.ROOT = original

print("OFFLINE RUNNER FAILURE PROPAGATION PASS")
