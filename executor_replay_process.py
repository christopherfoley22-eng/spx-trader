"""Process-resumable OFFLINE replay through DryRunExecutor.

Only SQLite controller state, its journal/market evidence, and a replay cursor
survive process death. Synthetic broker quantity and fill prices are rebuilt
from committed journal events on every fresh process start.
"""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3

from executor_dry_run import DryRunError, DryRunExecutor
from executor_replay import ReplayFormatError, ReplaySession, SimulationFillModel
from simulation_evidence import (
    EvidenceClock, EvidenceError, validate_broker_snapshot,
)


class ReplayRecoveryError(RuntimeError):
    pass


def _serial(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError("Replay evidence cannot be serialized")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    default=_serial).encode()).hexdigest()


class DurableReplayRunner:
    """Resume a validated replay by verifying durable audit before every step."""

    def __init__(self, session: ReplaySession, db_path, checkpoint=None):
        if not isinstance(session, ReplaySession):
            raise ReplayRecoveryError("Validated replay session required")
        self.session = session
        self.path = Path(db_path)
        self.checkpoint = checkpoint or (lambda label: None)
        self.intent_id = "replay:" + session.session_id
        self.input_digest = _digest(asdict(session))
        self._preflight_existing_file()
        first_day = session.events[0].now_wall.date().isoformat()
        try:
            self.controller = DryRunExecutor(
                self.path, session.account_id,
                lambda t: t.strftime("%Y%m%d") in session.expirations,
                session.max_exit_chunk, initial_trading_day=first_day)
            self.controller.db.execute("""
                CREATE TABLE IF NOT EXISTS replay_progress (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    session_id TEXT NOT NULL, input_digest TEXT NOT NULL,
                    event_count INTEGER NOT NULL, last_complete_index INTEGER NOT NULL
                )
            """)
            self._bind_progress()
            self._verify_progress()
            self._rebuild()
        except Exception as exc:
            if hasattr(self, "controller"):
                self.controller.close()
            if isinstance(exc, ReplayRecoveryError):
                raise
            raise ReplayRecoveryError("Durable replay evidence invalid: " + str(exc)) from exc

    def close(self):
        self.controller.close()

    def _preflight_existing_file(self):
        if not self.path.exists():
            return
        if self.path.stat().st_size == 0:
            raise ReplayRecoveryError("Persisted replay database is empty")
        try:
            probe = sqlite3.connect("file:{}?mode=ro".format(self.path), uri=True)
            try:
                if probe.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ReplayRecoveryError("Persisted replay database is corrupt")
                names = {row[0] for row in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                needed = {"controller", "chunks", "audit", "market_watermark",
                          "completed_trades", "replay_progress"}
                if not needed <= names:
                    raise ReplayRecoveryError("Persisted replay schema incomplete")
            finally:
                probe.close()
        except (sqlite3.DatabaseError, OSError) as exc:
            raise ReplayRecoveryError("Persisted replay database unreadable") from exc

    def _progress(self):
        return self.controller.db.execute(
            "SELECT session_id,input_digest,event_count,last_complete_index "
            "FROM replay_progress WHERE singleton=1").fetchone()

    def _bind_progress(self):
        old = self._progress()
        if old is None:
            if self.controller.status().phase != "FLAT" or self.controller.journal():
                raise ReplayRecoveryError("Existing controller lacks replay progress")
            self.controller.db.execute("INSERT INTO replay_progress VALUES (1,?,?,?,-1)",
                                       (self.session.session_id, self.input_digest,
                                        len(self.session.events)))
            return
        if old[0] == self.session.session_id:
            if old[1] != self.input_digest or old[2] != len(self.session.events):
                raise ReplayRecoveryError("Replay input changed after checkpoint")
            return
        if old[3] != old[2] - 1 or self.controller.status().phase != "FLAT":
            raise ReplayRecoveryError("Previous replay session is not complete and flat")
        prior_event = self.controller.db.execute(
            "SELECT rowid,detail FROM audit WHERE event_id=? AND kind='REPLAY_EVENT_COMPLETE'",
            ("replay-complete:" + old[0] + ":" + str(old[3]),)).fetchone()
        if prior_event is None:
            raise ReplayRecoveryError("Previous replay completion evidence missing")
        prior = self.controller.db.execute(
            "SELECT event_id,kind,detail FROM audit WHERE rowid<? ORDER BY rowid",
            (prior_event[0],)).fetchall()
        if json.loads(prior_event[1]).get("prior_digest") != _digest(prior):
            raise ReplayRecoveryError("Previous replay journal changed")
        self.controller.db.execute("UPDATE replay_progress SET session_id=?,input_digest=?,event_count=?,last_complete_index=-1 WHERE singleton=1",
                                   (self.session.session_id, self.input_digest,
                                    len(self.session.events)))

    def _verify_progress(self):
        session_id, digest, count, cursor = self._progress()
        if (session_id != self.session.session_id or digest != self.input_digest
                or count != len(self.session.events) or not -1 <= cursor < count):
            raise ReplayRecoveryError("Replay cursor contradicts input")
        for index in range(count):
            row = self.controller.db.execute(
                "SELECT rowid,detail FROM audit WHERE event_id=? AND kind='REPLAY_EVENT_COMPLETE'",
                (self._complete_id(index),)).fetchone()
            if (index <= cursor) != (row is not None):
                raise ReplayRecoveryError("Replay cursor and journal tail disagree")
            if row is not None:
                prior = self.controller.db.execute(
                    "SELECT event_id,kind,detail FROM audit WHERE rowid<? ORDER BY rowid",
                    (row[0],)).fetchall()
                expected = {"index": index, "sequence": self.session.events[index].sequence,
                            "input_digest": digest, "prior_digest": _digest(prior)}
                if json.loads(row[1]) != expected:
                    raise ReplayRecoveryError("Replay completion audit corrupted")
        return cursor

    def _complete_id(self, index):
        return "replay-complete:{}:{}".format(self.session.session_id, index)

    def _journal_event(self, event_id, kind=None):
        row = self.controller.db.execute(
            "SELECT kind,detail FROM audit WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        if kind is not None and row[0] != kind:
            raise ReplayRecoveryError("Journal event ID has wrong type")
        return json.loads(row[1])

    def _rebuild(self):
        self.model = SimulationFillModel()
        self.clock = EvidenceClock()
        self.plan = None
        row = self.controller._row()
        if row[3] is not None:
            self.plan = self.controller._plan(row[3])
        else:
            completed = next((x for x in self.controller.completed_trades()
                              if x[0] == self.intent_id), None)
            if completed:
                self.plan = self.controller._plan(completed[6])
        for event_id, kind, raw in self.controller.journal():
            if kind not in {"ENTRY_EVENT", "EXIT_EVENT"}:
                continue
            detail = json.loads(raw)
            side = "ENTRY" if kind == "ENTRY_EVENT" else "EXIT"
            if not detail["chunk"].startswith(self.intent_id + ":" + side.lower() + ":"):
                continue
            qty = detail["fill"]
            if qty:
                if detail.get("simulated_price") is None:
                    raise ReplayRecoveryError("Committed fill lacks simulated price evidence")
                self.model.filled(side, detail["con_id"], qty,
                                  detail["simulated_price"], event_id, detail["chunk"])
        status = self.controller.status()
        if self.model.quantity != status.owned or self.model.con_id != status.con_id:
            raise ReplayRecoveryError("Journal fill ledger contradicts persisted broker state")
        if status.phase in {"ENTERING", "OPEN", "EXITING"} and self.plan is None:
            raise ReplayRecoveryError("Active lifecycle lacks authorized plan")
        if status.phase == "OPEN" and self._journal_event("open:" + self.intent_id,
                                                           "OPEN_CONFIRMED") is None:
            raise ReplayRecoveryError("OPEN lacks confirmation journal")
        if status.phase == "EXITING" and self._journal_event("exit:" + self.intent_id,
                                                              "EXIT_TRIGGERED") is None:
            raise ReplayRecoveryError("EXITING lacks durable trigger")
        if status.ride and self._journal_event("ride:" + self.intent_id,
                                               "LET_IT_RIDE") is None:
            raise ReplayRecoveryError("LET_IT_RIDE state lacks journal evidence")
        if status.phase == "FLAT" and self._journal_event(
                "intent:" + self.intent_id, "INTENT_ACCEPTED") is not None:
            completed = any(x[0] == self.intent_id for x in self.controller.completed_trades())
            flat = self._journal_event("flat:" + self.intent_id, "CONFIRMED_FLAT")
            empty = self._journal_event("entry-empty:" + self.intent_id, "ENTRY_REJECTED")
            if completed != (flat is not None) or (not completed and empty is None):
                raise ReplayRecoveryError("Local flat lacks matching completion evidence")
        for key, source in self.controller.db.execute(
                "SELECT key,source_time FROM market_watermark"):
            parsed_key = ("OPTION", int(key[7:])) if key.startswith("OPTION:") else key
            self.clock.last_source[parsed_key] = datetime.fromisoformat(source)

    def _snapshot(self, event):
        if event.broker is None:
            raise ReplayRecoveryError("Broker evidence missing")
        snapshot = (self.model.snapshot(self.session.account_id, event.monotonic)
                    if event.broker == "SIMULATOR" else event.broker)
        validate_broker_snapshot(snapshot, self.session.account_id, event.monotonic)
        if (snapshot.position_qty != self.model.quantity or
                snapshot.con_id != self.model.con_id or snapshot.open_order_count):
            raise ReplayRecoveryError("Broker observation contradicts durable fill ledger")
        return snapshot

    def _quote(self, event):
        if self.plan is None or event.spx is None:
            raise ReplayRecoveryError("Exact option or SPX observation missing")
        option = next((x for x in event.options
                       if x.contract.con_id == self.plan["con_id"]), None)
        if option is None or asdict(option.contract) != self.plan["contract"]:
            raise ReplayRecoveryError("Exact authorized option quote missing or changed")
        self.clock.validate_pair(
            self.session.direction, event.spx, option, event.now_wall,
            event.monotonic,
            lambda t: t.strftime("%Y%m%d") in self.session.expirations)
        return option

    def _mark_complete(self, index):
        event = self.session.events[index]
        db = self.controller.db
        db.execute("BEGIN IMMEDIATE")
        try:
            for key, source in self.clock.last_source.items():
                name = "OPTION:" + str(key[1]) if isinstance(key, tuple) else key
                db.execute("INSERT INTO market_watermark(key,source_time) VALUES (?,?) "
                           "ON CONFLICT(key) DO UPDATE SET source_time=excluded.source_time",
                           (name, source.isoformat()))
            prior = db.execute("SELECT event_id,kind,detail FROM audit ORDER BY rowid").fetchall()
            self.controller._event(self._complete_id(index), "REPLAY_EVENT_COMPLETE",
                                   {"index": index, "sequence": event.sequence,
                                    "input_digest": self.input_digest,
                                    "prior_digest": _digest(prior)})
            db.execute("UPDATE replay_progress SET last_complete_index=? WHERE singleton=1",
                       (index,))
            self.controller._check_state()
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        self.checkpoint("{}:event_complete".format(event.sequence))

    def _fill_entry(self, event, chunk_id, qty, rejected, event_id):
        old = self._journal_event(event_id, "ENTRY_EVENT")
        option = self._quote(event) if qty else None
        price = str(Decimal(str(option.ask))) if qty else None
        if old is not None:
            if (old["chunk"] != chunk_id or old["fill"] != qty
                    or old["rejected"] != rejected or old.get("simulated_price") != price):
                raise ReplayRecoveryError("Conflicting duplicate entry callback")
            return
        self.checkpoint("{}:before_entry_fill:{}".format(event.sequence, chunk_id.rsplit(":", 1)[-1]))
        self.controller.entry_event(event_id, chunk_id, qty, rejected,
            fill_con_id=self.plan["con_id"] if qty else None,
            simulated_price=option.ask if qty else None,
            account=event.account, spx=event.spx, option=option,
            snapshot=self._snapshot(event), now_wall=event.now_wall,
            now_monotonic=event.monotonic)
        self.checkpoint("{}:entry_committed:{}".format(event.sequence, chunk_id.rsplit(":", 1)[-1]))
        if qty:
            self.model.filled("ENTRY", self.plan["con_id"], qty, option.ask,
                              event_id, chunk_id)
        self.checkpoint("{}:entry_fill:{}".format(event.sequence, chunk_id.rsplit(":", 1)[-1]))

    def _fill_exit(self, event, chunk_id, qty, rejected, event_id):
        old = self._journal_event(event_id, "EXIT_EVENT")
        option = self._quote(event) if qty else None
        price = str(Decimal(str(option.bid))) if qty else None
        if old is not None:
            if (old["chunk"] != chunk_id or old["fill"] != qty
                    or old["rejected"] != rejected or old.get("simulated_price") != price):
                raise ReplayRecoveryError("Conflicting duplicate exit callback")
            return
        self.checkpoint("{}:before_exit_fill:{}".format(event.sequence, chunk_id.rsplit(":", 1)[-1]))
        self.controller.exit_event(event_id, chunk_id, qty, rejected,
            fill_con_id=self.plan["con_id"] if qty else None,
            simulated_price=option.bid if qty else None,
            snapshot=self._snapshot(event), now_monotonic=event.monotonic)
        self.checkpoint("{}:exit_committed:{}".format(event.sequence, chunk_id.rsplit(":", 1)[-1]))
        if qty:
            self.model.filled("EXIT", self.plan["con_id"], qty, option.bid,
                              event_id, chunk_id)
        self.checkpoint("{}:exit_fill:{}".format(event.sequence, chunk_id.rsplit(":", 1)[-1]))

    def _process_event(self, index):
        event = self.session.events[index]
        seq = event.sequence
        snapshot = self._snapshot(event)
        if seq == self.session.intent_sequence:
            accepted = self._journal_event("intent:" + self.intent_id, "INTENT_ACCEPTED")
            if accepted is None:
                self.checkpoint("{}:before_intent".format(seq))
                if self.controller.press(self.intent_id, self.session.direction, snapshot,
                                         event.now_wall, event.monotonic) != "ACCEPTED":
                    raise ReplayRecoveryError("Intent rejected by durable daily or state gate")
                self.checkpoint("{}:intent".format(seq))
        if self.controller.status().phase == "INTENT":
            if event.spx is None or event.account is None or not event.options:
                raise ReplayRecoveryError("Authorization evidence missing")
            self.plan = self.controller.authorize(
                self.intent_id, event.account, event.spx, event.options,
                self._snapshot(event), event.now_wall, event.monotonic)
            self.checkpoint("{}:authorized".format(seq))
        if self.controller.status().phase == "ENTERING":
            if self.session.fill_mode == "AUTO":
                while self.controller.entry_chunks():
                    chunk_id, qty = self.controller.entry_chunks()[0]
                    self._fill_entry(event, chunk_id, qty, False, "sim-buy:" + chunk_id)
            else:
                for fill in (x for x in event.fills if x["side"] == "ENTRY"):
                    chunk_id = self.intent_id + ":entry:" + str(fill["chunk_index"])
                    self._fill_entry(event, chunk_id, fill["quantity"],
                                     fill["rejected"], fill["event_id"])
            if not self.controller.entry_chunks():
                self.controller.finish_entry(self._snapshot(event), event.monotonic)
                self.checkpoint("{}:open".format(seq))
        if self.controller.status().phase in {"OPEN", "EXITING"}:
            if event.spx is None:
                raise ReplayRecoveryError("Underlying feed missing while position active")
            market_id = self.intent_id + ":spx:" + str(seq)
            if self._journal_event(market_id, "MARKET_OBSERVED") is None:
                self.checkpoint("{}:before_market".format(seq))
                before = self.controller.status()
                self.controller.observe(market_id, event.spx, self._snapshot(event),
                                        event.now_wall, event.monotonic)
                after = self.controller.status()
                if after.peak != before.peak:
                    self.checkpoint("{}:high_water".format(seq))
                if self._journal_event("near:" + self.intent_id, "NEAR_WINNER_ARMED"):
                    if before.peak != after.peak:
                        self.checkpoint("{}:near_winner".format(seq))
                if after.ride and not before.ride:
                    self.checkpoint("{}:let_it_ride".format(seq))
                if after.phase == "EXITING" and before.phase == "OPEN":
                    self.checkpoint("{}:exiting".format(seq))
            if event.options:
                self._quote(event)
        if self.controller.status().phase == "EXITING":
            if self.session.fill_mode == "AUTO":
                while self.controller.status().owned:
                    chunk_id, qty = self.controller.next_exit_chunk(
                        self._snapshot(event), event.monotonic)
                    self._fill_exit(event, chunk_id, qty, False, "sim-sell:" + chunk_id)
            else:
                for fill in (x for x in event.fills if x["side"] == "EXIT"):
                    old = self._journal_event(fill["event_id"], "EXIT_EVENT")
                    if old is not None:
                        chunk_id = old["chunk"]
                    else:
                        chunk = self.controller.next_exit_chunk(
                            self._snapshot(event), event.monotonic)
                        if chunk is None or not chunk[0].endswith(":exit:" + str(fill["chunk_index"])):
                            raise ReplayRecoveryError("Scripted exit chunk out of order")
                        chunk_id = chunk[0]
                    self._fill_exit(event, chunk_id, fill["quantity"],
                                    fill["rejected"], fill["event_id"])
            if self.controller.status().owned == 0:
                self.checkpoint("{}:before_flat".format(seq))
                self.controller.confirm_flat(self._snapshot(event), event.monotonic)
                self.checkpoint("{}:flat".format(seq))
        for fill in event.fills:
            expected = "ENTRY_EVENT" if fill["side"] == "ENTRY" else "EXIT_EVENT"
            if self._journal_event(fill["event_id"], expected) is None:
                raise ReplayRecoveryError("Scripted fill was not applied")

    def run(self, start_index=None):
        cursor = self._verify_progress()
        if start_index is None:
            start_index = cursor + 1
        if not isinstance(start_index, int) or start_index < 0 or start_index > cursor + 1:
            raise ReplayRecoveryError("Replay attempted to skip an unprocessed event")
        for index in range(start_index, len(self.session.events)):
            if index <= cursor:
                # Exact re-delivery after restart is a no-op by durable cursor.
                continue
            self._rebuild()
            self._process_event(index)
            self._mark_complete(index)
            cursor = index
        return self.result()

    def result(self):
        self._verify_progress()
        self._rebuild()
        status = self.controller.status()
        journal = self.controller.journal()
        prefix = self.intent_id
        kinds = [kind for eid, kind, _ in journal if
                 eid in {"intent:" + prefix, "plan:" + prefix,
                         "open:" + prefix, "exit:" + prefix, "flat:" + prefix}]
        trigger = self._journal_event("exit:" + prefix, "EXIT_TRIGGERED")
        digest = _digest([(eid, kind, detail) for eid, kind, detail in journal])
        economics = _digest([(eid, kind, detail) for eid, kind, detail in journal
                            if kind in {"ENTRY_EVENT", "EXIT_EVENT", "TRADE_COUNTED"}
                            and (eid.startswith("sim-") and prefix in eid
                                 or (kind == "TRADE_COUNTED" and eid == "count:" + prefix)
                                 or (kind in {"ENTRY_EVENT", "EXIT_EVENT"}
                                     and json.loads(detail)["chunk"].startswith(prefix + ":")))])
        return {"session_id": self.session.session_id,
                "phase": status.phase, "position_qty": status.owned,
                "broker_qty": self.model.quantity, "con_id": status.con_id,
                "trade_count": status.trades_today,
                "strategy_transitions": kinds,
                "exit_reason": trigger["reason"] if trigger else None,
                "entry_fills": self.model.entry_fills,
                "exit_fills": self.model.exit_fills,
                "journal_digest": digest, "economic_digest": economics,
                "last_complete_index": self._progress()[3]}
