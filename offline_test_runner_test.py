"""The runner must report script failures and exclude broker-connected names."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
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

# The heavier generated replay gets more process time; other tests retain
# their budget and the connected manifest remains excluded from dispatch.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    for name in runner.IBKR_CONNECTED:
        (root / name).write_text("raise RuntimeError('MUST NOT RUN')\n")
    (root / "executor_replay_stress_test.py").write_text("assert True\n")
    (root / "ordinary_test.py").write_text("assert True\n")
    calls = {}
    original_root, original_run = runner.ROOT, runner.subprocess.run
    def capture(command, **kwargs):
        calls[Path(command[-1]).name] = kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    try:
        runner.ROOT, runner.subprocess.run = root, capture
        assert runner.main() == 0
        assert calls == {"executor_replay_stress_test.py": 180, "ordinary_test.py": 60}
    finally:
        runner.ROOT, runner.subprocess.run = original_root, original_run

print("OFFLINE PROCESS BUDGET AND BROKER EXCLUSION PASS")
assert runner.PROCESS_TIMEOUTS['executor_liquidity_stress_test.py']==180
