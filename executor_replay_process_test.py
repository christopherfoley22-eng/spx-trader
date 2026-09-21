"""Fresh-interpreter crash/restart regression for synthetic replay only."""

import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from executor_replay_sessions import make_session

WORKER = Path(__file__).with_name("executor_replay_process_worker.py")


def session(name, direction="CALL", offset=0):
    raw = make_session(name, direction, [0, 1, 4.8, 4.99, 5, 10, 7.01, 7, 7],
                       fill_mode="SCRIPTED", start_offset_seconds=offset)
    for index, side, chunk, qty in ((0, "ENTRY", 0, 4),
                                     (1, "ENTRY", 0, 6), (1, "ENTRY", 1, 1),
                                     (8, "EXIT", 0, 3),
                                     (9, "EXIT", 0, 7), (9, "EXIT", 1, 1)):
        raw["events"][index]["fills"].append({
            "event_id": "{}:{}:{}:{}".format(name, side, index, chunk),
            "side": side, "chunk_index": chunk, "quantity": qty,
            "rejected": False})
    return raw


class ProcessRestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def source(self, raw):
        path = self.root / (raw["session_id"] + ".json")
        path.write_text(json.dumps(raw, sort_keys=True))
        return path

    def invoke(self, source, db, *, stop=None, start=None):
        args = [sys.executable, "-B", str(WORKER), str(source), str(db)]
        if stop is not None:
            args += ["--stop", stop]
        if start is not None:
            args += ["--start-index", str(start)]
        return subprocess.run(args, capture_output=True, text=True, timeout=20)

    def success(self, source, db, **kwargs):
        result = self.invoke(source, db, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def killed(self, source, db, label):
        result = self.invoke(source, db, stop=label)
        self.assertEqual(result.returncode, 73, (label, result.stdout, result.stderr))

    def test_each_lifecycle_checkpoint_matches_uninterrupted(self):
        labels = (
            "0:intent", "0:authorized", "0:before_entry_fill:0",
            "0:entry_committed:0", "0:entry_fill:0",
            "1:entry_fill:1", "1:open", "2:before_market",
            "3:near_winner", "4:before_market", "5:let_it_ride",
            "6:high_water", "8:before_market", "8:exiting",
            "8:exit_committed:0", "8:exit_fill:0", "9:exit_fill:1",
            "9:before_flat", "9:flat")
        for direction in ("CALL", "PUT"):
            with self.subTest(direction=direction):
                src = self.source(session(direction.lower(), direction))
                control = self.success(src, self.root / (direction + "-control.db"))
                self.assertEqual(control["phase"], "FLAT")
                self.assertEqual(control["trade_count"], 1)
                for number, label in enumerate(labels):
                    with self.subTest(direction=direction, checkpoint=label):
                        db = self.root / (direction + str(number) + ".db")
                        self.killed(src, db, label)
                        self.assertEqual(self.success(src, db), control)

    def test_repeated_process_deaths_and_duplicate_cursor_delivery(self):
        src = self.source(session("repeated"))
        control = self.success(src, self.root / "control.db")
        db = self.root / "repeated.db"
        for label in ("0:intent", "0:authorized", "0:entry_committed:0",
                      "1:open", "3:near_winner", "5:let_it_ride",
                      "8:exiting", "8:exit_committed:0", "9:before_flat"):
            self.killed(src, db, label)
        self.assertEqual(self.success(src, db), control)
        # Completed input can be redelivered one or more events early.
        self.assertEqual(self.success(src, db, start=8), control)
        self.assertEqual(self.success(src, db, start=9), control)

    def test_cursor_skip_is_blocked_without_changing_state(self):
        src = self.source(session("cursor"))
        db = self.root / "cursor.db"
        self.killed(src, db, "3:event_complete")
        with sqlite3.connect(db) as conn:
            cursor = conn.execute("SELECT last_complete_index FROM replay_progress").fetchone()[0]
        self.assertEqual(cursor, 3)
        blocked = self.invoke(src, db, start=5)
        self.assertEqual(blocked.returncode, 3)
        self.assertEqual(self.success(src, db, start=2)["phase"], "FLAT")

    def test_truncated_corrupt_and_journal_disagreement_fail_closed(self):
        src = self.source(session("damage"))
        for number, damage in enumerate(("truncate", "count", "open_audit",
                                         "cursor", "prefix_audit")):
            with self.subTest(damage=damage):
                db = self.root / (str(number) + ".db")
                self.killed(src, db, "1:open")
                if damage == "truncate":
                    with db.open("r+b") as handle:
                        handle.truncate(16)
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(db) + suffix)
                        if sidecar.exists():
                            sidecar.unlink()
                else:
                    with sqlite3.connect(db) as conn:
                        if damage == "count":
                            conn.execute("UPDATE controller SET trades_today=0")
                        elif damage == "open_audit":
                            conn.execute("DELETE FROM audit WHERE kind='OPEN_CONFIRMED'")
                        elif damage == "prefix_audit":
                            conn.execute("UPDATE audit SET detail='{}' WHERE kind='INTENT_ACCEPTED'")
                        else:
                            conn.execute("UPDATE replay_progress SET last_complete_index=8")
                self.assertEqual(self.invoke(src, db).returncode, 3)

    def test_contradictory_broker_evidence_blocks_resume(self):
        raw = session("broker")
        event = raw["events"][1]
        event["broker"] = {
            "account_id": raw["account_id"],
            "selected_account": raw["account_id"],
            "managed_accounts": [raw["account_id"]], "complete": True,
            "position_qty": 4, "con_id": 999,
            "open_order_count": 0, "received_monotonic": event["monotonic"]}
        src = self.source(raw)
        db = self.root / "broker.db"
        self.killed(src, db, "0:entry_fill:0")
        self.assertEqual(self.invoke(src, db).returncode, 3)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT broker_qty FROM controller").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT last_complete_index FROM replay_progress").fetchone()[0], 0)

    def test_local_flat_without_completion_audit_is_not_trusted(self):
        src = self.source(session("flat-audit"))
        db = self.root / "flat-audit.db"
        self.killed(src, db, "9:flat")
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM audit WHERE kind='CONFIRMED_FLAT'")
        self.assertEqual(self.invoke(src, db).returncode, 3)

    def test_two_trades_cap_survives_fresh_processes(self):
        db = self.root / "day.db"
        first = self.source(session("day-one"))
        self.killed(first, db, "9:flat")
        self.assertEqual(self.success(first, db)["trade_count"], 1)
        second = self.source(session("day-two", offset=20))
        self.killed(second, db, "0:intent")
        second_result = self.success(second, db)
        self.assertEqual(second_result["trade_count"], 2)
        third = self.source(session("day-three", offset=40))
        self.assertEqual(self.invoke(third, db).returncode, 3)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT trades_today FROM controller").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM completed_trades").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
