"""Run every offline standalone test script; no IBKR connection is attempted."""

from pathlib import Path
import ast
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
# This generated suite performs many durable SQLite restart/replay sessions.
# Its process budget is independent of every market freshness/safety assertion.
PROCESS_TIMEOUTS = {"executor_replay_stress_test.py": 180,
                    "executor_execution_twin_test.py": 180,
                    "executor_liquidity_stress_test.py": 180}
IBKR_CONNECTED = {
    "connection_test.py",
    "spx_live_test.py",
    "spx_contract_test.py",
    "spx_chain_test.py",
    "ibkr_reconcile_test.py",
}


def main():
    scripts = sorted(ROOT.glob("*_test.py"))
    found = {path.name for path in scripts}
    if not IBKR_CONNECTED <= found:
        print("Offline exclusion manifest is stale", file=sys.stderr)
        return 2
    offline = [path for path in scripts if path.name not in IBKR_CONNECTED]
    for script in offline:
        tree = ast.parse(script.read_text(), filename=str(script))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ibapi"):
                print("Unexpected IBKR import in offline script: " + script.name, file=sys.stderr)
                return 2
            if isinstance(node, ast.Import) and any(alias.name.startswith("ibapi") for alias in node.names):
                print("Unexpected IBKR import in offline script: " + script.name, file=sys.stderr)
                return 2
    failures = []
    for script in offline:
        timeout = PROCESS_TIMEOUTS.get(script.name, 60)
        try:
            result = subprocess.run(
                [sys.executable, "-B", str(script)], cwd=ROOT,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            failures.append((script.name, "Timed out after {} seconds".format(timeout)))
            continue
        if result.returncode:
            failures.append((script.name, (result.stdout + result.stderr)[-4000:]))
    for name, detail in failures:
        print("FAIL {}\n{}".format(name, detail), file=sys.stderr)
    print("Offline scripts: {} passed, {} failed; IBKR-connected scripts excluded: {}".format(
        len(offline) - len(failures), len(failures), len(IBKR_CONNECTED)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
