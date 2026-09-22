"""Offline direction-only local lifecycle and recovery tests."""

import json
from pathlib import Path
import sqlite3
import tempfile
import uuid

from executor_local_service import LocalDryRunService, LocalServiceError
from executor_replay import ReplaySession


def request_id():
    return str(uuid.uuid4())


def audit(path):
    with sqlite3.connect(str(path)) as db:
        return [(kind, json.loads(detail)) for kind, detail in
                db.execute("SELECT kind,detail FROM audit ORDER BY rowid")]


# Existing local simulation state is migrated without resetting request history.
with tempfile.TemporaryDirectory() as directory:
    old_path = Path(directory) / "service.sqlite"
    with sqlite3.connect(old_path) as db:
        db.execute("CREATE TABLE settings (singleton INTEGER PRIMARY KEY, fixture TEXT NOT NULL)")
        db.execute("INSERT INTO settings VALUES (1,'immediate_winner')")
        db.execute("CREATE TABLE requests (request_id TEXT PRIMARY KEY, direction TEXT NOT NULL, session_json TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0)")
    migrated = LocalDryRunService(directory)
    with sqlite3.connect(old_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
    assert "automatic" in columns
    migrated.close()


# One direction selection drives the entire CALL and PUT lifecycle.
for direction in ("CALL", "PUT"):
    with tempfile.TemporaryDirectory() as directory:
        service = LocalDryRunService(directory)
        result = service.execute(direction, request_id())
        status = result["status"]
        assert result["result"] == "ACCEPTED" and result["automatic"] is True
        assert status["lifecycle"] == "FLAT"
        assert status["state"] == "CONFIRMED FLAT"
        assert status["trade_count"] == 1 and status["ready"] is True
        assert status["position"] is None
        rows = audit(service.executor_path)
        kinds = [kind for kind, _ in rows]
        for required in ("INTENT_ACCEPTED", "EVIDENCE_VALIDATED", "PLAN_AUTHORIZED",
                         "ENTRY_CHUNK_PLANNED", "ENTRY_EVENT", "OPEN_CONFIRMED",
                         "PROFIT_PROTECTION_ARMED", "LET_IT_RIDE", "EXIT_TRIGGERED",
                         "EXIT_CHUNK_PLANNED", "EXIT_EVENT", "RECONCILED",
                         "CONFIRMED_FLAT"):
            assert required in kinds, required
        plan = next(detail for kind, detail in rows if kind == "PLAN_AUTHORIZED")
        assert plan["quantity"] == 11 and plan["chunks"] == [10, 1]
        assert sum(item["fill"] for kind, item in rows if kind == "ENTRY_EVENT") == 11
        assert sum(item["fill"] for kind, item in rows if kind == "EXIT_EVENT") == 11
        trigger = next(item for kind, item in rows if kind == "EXIT_TRIGGERED")
        assert trigger["reason"] == "LET_IT_RIDE_REVERSAL"
        assert trigger["reversal"] == "3.0"
        with sqlite3.connect(service.executor_path) as db:
            controller = db.execute(
                "SELECT phase,broker_qty,trades_today FROM controller").fetchone()
            assert controller == ("FLAT", 0, 1)
            assert db.execute("SELECT count(*) FROM completed_trades").fetchone()[0] == 1
        service.close()


# The two-trade New York-date cap survives complete service restart.
with tempfile.TemporaryDirectory() as directory:
    service = LocalDryRunService(directory)
    service.execute("CALL", request_id())
    service.close()
    service = LocalDryRunService(directory)
    service.execute("PUT", request_id())
    service.close()
    service = LocalDryRunService(directory)
    assert service.status()["trade_count"] == 2
    assert service.status()["ready"] is False
    try:
        service.execute("CALL", request_id())
    except LocalServiceError:
        pass
    else:
        raise AssertionError("Third automatic trade was accepted")
    assert service.status()["trade_count"] == 2
    service.close()


# A persisted automatic request resumes from every replay boundary in a fresh
# service instance without another user intent or duplicate economic action.
with tempfile.TemporaryDirectory() as seed_directory:
    seed = LocalDryRunService(seed_directory)
    raw = seed._raw(request_id(), "CALL", 0)
    event_count = len(ReplaySession.from_dict(raw).events)
    seed.close()

for stop_index in range(event_count):
    with tempfile.TemporaryDirectory() as directory:
        service = LocalDryRunService(directory)
        rid = request_id()
        raw = service._raw(rid, "CALL", 0)
        row = (rid, "CALL", json.dumps(raw, sort_keys=True), 1)
        service.db.execute(
            "INSERT INTO requests(request_id,direction,session_json,completed,automatic) "
            "VALUES (?,?,?,0,1)", row[:3])
        service._progress(row, stop_index=stop_index)
        service.close()

        restarted = LocalDryRunService(directory)
        recovered = restarted.status()
        assert recovered["lifecycle"] == "FLAT", (stop_index, recovered)
        assert recovered["trade_count"] == 1
        first_journal = audit(restarted.executor_path)
        assert [kind for kind, _ in first_journal].count("CONFIRMED_FLAT") == 1
        assert [kind for kind, _ in first_journal].count("TRADE_COUNTED") == 1
        assert restarted.status() == recovered
        assert audit(restarted.executor_path) == first_journal
        restarted.close()


# Corrupt automatic recovery evidence fails closed and cannot accept an intent.
with tempfile.TemporaryDirectory() as directory:
    service = LocalDryRunService(directory)
    rid = request_id()
    raw = service._raw(rid, "CALL", 0)
    service.db.execute(
        "INSERT INTO requests(request_id,direction,session_json,completed,automatic) "
        "VALUES (?,?,?,0,1)", (rid, "CALL", json.dumps(raw, sort_keys=True)))
    Path(service.executor_path).write_bytes(b"truncated")
    status = service.status()
    assert status["state"] == "RECOVERY REQUIRED" and status["ready"] is False
    try:
        service.execute("PUT", request_id())
    except LocalServiceError:
        pass
    else:
        raise AssertionError("Intent accepted with corrupt recovery evidence")
    service.close()


# The normal UI path has no SELL/quantity/strike/expiration/exit controls.
ui = Path(__file__).with_name("executor_local_ui.html").read_text()
web = Path(__file__).with_name("executor_local_web.py").read_text()
assert "'/api/execute'" in ui
assert 'self.path == "/api/execute"' in web
for forbidden in ("/api/sell", "/api/exit", 'name="quantity"',
                  'name="strike"', 'name="expiration"', "manual sell"):
    assert forbidden not in ui.lower()

print("AUTOMATIC LOCAL CALL/PUT TO CONFIRMED-FLAT RECOVERY PASS")
