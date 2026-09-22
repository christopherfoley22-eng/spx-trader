"""Canonical versioned replay fixtures and exact strategy boundaries."""

import json
from pathlib import Path

from executor_replay import ReplayRunner, ReplaySession
from executor_replay_sessions import make_session

fixtures = json.loads(Path(__file__).with_name("executor_replay_fixtures.json").read_text())
assert fixtures["version"] == 1
assert len(fixtures["cases"]) == 15

for case in fixtures["cases"]:
    for direction in ("CALL", "PUT"):
        name = case["name"] + "_" + direction.lower()
        session = ReplaySession.from_dict(make_session(name, direction, case["favorable"]))
        runner = ReplayRunner(session)
        baseline = runner.run()
        assert json.loads(json.dumps(baseline)) == baseline
        repeat = runner.run()
        restarted = runner.run(restart_after_actions=True)
        at_sequences = runner.run(restart_sequences=(0, 1, 2))
        assert baseline == repeat, name
        assert baseline["simulation_only"] is True
        assert baseline["final_reconciliation"] == case["final"], (name, baseline)
        assert baseline["authorized_quantity"] == 11
        assert baseline["trade_count"] == 1
        assert baseline["selected_contract"]["right"] == ("C" if direction == "CALL" else "P")
        assert baseline["exit_trigger"] is None if case["reason"] is None else baseline["exit_trigger"]["reason"] == case["reason"]
        assert baseline["journal_digest"] == restarted["journal_digest"], name
        assert baseline["journal_digest"] == at_sequences["journal_digest"], name
        assert baseline["entry_fills"] == restarted["entry_fills"]
        assert baseline["exit_fills"] == restarted["exit_fills"]
        assert baseline["final_reconciliation"] == restarted["final_reconciliation"]
        assert restarted["restart_count"] >= 3
        if case["final"] == "CONFIRMED_FLAT":
            assert baseline["hypothetical_option_pnl_usd"] is not None
            assert sum(x["quantity"] for x in baseline["entry_fills"]) == 11
            assert sum(x["quantity"] for x in baseline["exit_fills"]) == 11
            assert baseline["safety_blocks"] == []
        else:
            assert baseline["hypothetical_option_pnl_usd"] is None
            assert baseline["exit_fills"] == []
            assert baseline["safety_blocks"][-1]["reason"] == "SESSION_ENDED_OPEN"

print("REPLAY CANONICAL FIXTURES PASS: 15 cases, CALL and PUT mirrored")
