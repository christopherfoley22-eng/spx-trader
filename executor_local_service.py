"""Local, synthetic-only control surface around the durable dry-run Executor.

No IBKR imports, sockets to a broker, or order methods exist here. Replay
observations are the only source of account, market, and broker evidence.
"""

from datetime import datetime
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from executor_dry_run import DryRunExecutor
from executor_replay import ReplaySession
from executor_replay_process import DurableReplayRunner
from executor_replay_sessions import ACCOUNT, BASE, make_session
from mortificatio_v01 import OptionQuote, build_entry_plan
from simulation_evidence import (
    EvidenceClock, VerifiedBrokerSnapshot, conservative_usable_funds,
    validate_broker_snapshot,
)

FIXTURES = json.loads(Path(__file__).with_name("executor_replay_fixtures.json").read_text())["cases"]
PATHS = {item["name"]: item["favorable"] for item in FIXTURES}
REQUEST_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class LocalServiceError(RuntimeError):
    def __init__(self, message, code="SAFETY_BLOCKED", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LocalDryRunService:
    """Single local writer; SQLite request registry survives service restart."""

    def __init__(self, state_dir, session_factory=None):
        self.root = Path(state_dir)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.lock_file = (self.root / "service.lock").open("a+b")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock_file.close()
            raise LocalServiceError("Another local Executor service owns this state directory") from exc
        self.db_path = self.root / "service.sqlite"
        self.executor_path = self.root / "executor.sqlite"
        self.lock = threading.RLock()
        self.factory = session_factory or (lambda name, direction, path, offset:
                                           make_session(name, direction, path,
                                                        start_offset_seconds=offset))
        self.db = sqlite3.connect(str(self.db_path), isolation_level=None,
                                  check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (singleton INTEGER PRIMARY KEY CHECK(singleton=1), fixture TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, direction TEXT NOT NULL, session_json TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (1,'immediate_winner')")
        self.db_path.chmod(0o600)

    def close(self):
        self.db.close()
        if not self.lock_file.closed:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()

    def _fixture(self):
        row = self.db.execute("SELECT fixture FROM settings WHERE singleton=1").fetchone()
        if row is None or row[0] not in PATHS:
            raise LocalServiceError("Demo configuration requires recovery")
        return row[0]

    def _raw(self, request_id, direction, count):
        return self.factory(request_id, direction, PATHS[self._fixture()], count * 10)

    def _active(self):
        rows = self.db.execute("SELECT request_id,direction,session_json FROM requests WHERE completed=0 ORDER BY rowid").fetchall()
        if len(rows) > 1:
            raise LocalServiceError("Contradictory active request registry")
        return rows[0] if rows else None

    def _controller(self):
        if not self.executor_path.exists():
            return None
        return DryRunExecutor(self.executor_path, ACCOUNT,
                              lambda t: t.strftime("%Y%m%d") == BASE.strftime("%Y%m%d"),
                              initial_trading_day=BASE.date().isoformat())

    def _verify_previous_completion(self):
        prior = self.db.execute(
            "SELECT request_id,direction,session_json FROM requests WHERE completed=1 ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if not self.executor_path.exists():
            if prior is not None:
                raise LocalServiceError("Completed request lacks persisted Executor evidence")
            return
        if prior is None:
            raise LocalServiceError("Persisted Executor has no verified request history")
        try:
            runner = DurableReplayRunner(self._session(prior), self.executor_path)
            try:
                result = runner.result()
                if (result["last_complete_index"] != len(runner.session.events) - 1
                        or runner.controller.status().phase != "FLAT"):
                    raise LocalServiceError("Previous simulated lifecycle is not confirmed complete")
            finally:
                runner.close()
        except Exception as exc:
            raise LocalServiceError("Previous replay evidence requires recovery") from exc

    def _evidence_ready(self, direction, count):
        raw = self._raw("readiness", direction, count)
        session = ReplaySession.from_dict(raw)
        event = session.events[0]
        broker = (VerifiedBrokerSnapshot(ACCOUNT, ACCOUNT, (ACCOUNT,), True,
                                         0, None, 0, event.monotonic)
                  if event.broker == "SIMULATOR" else event.broker)
        validate_broker_snapshot(broker, ACCOUNT, event.monotonic)
        if broker.position_qty != 0 or broker.con_id is not None or broker.open_order_count:
            raise LocalServiceError("Synthetic broker reconciliation disagrees with flat state")
        funds = conservative_usable_funds(event.account, ACCOUNT, event.monotonic)
        if event.spx is None or not event.options:
            raise LocalServiceError("Complete synthetic market evidence unavailable")
        clock = EvidenceClock()
        if self.executor_path.exists():
            with sqlite3.connect(str(self.executor_path)) as conn:
                for key, source in conn.execute("SELECT key,source_time FROM market_watermark"):
                    clock.last_source[("OPTION", int(key[7:])) if key.startswith("OPTION:")
                                      else key] = datetime.fromisoformat(source)
        for option in event.options:
            clock.validate_pair(direction, event.spx, option, event.now_wall,
                                event.monotonic,
                                lambda t: t.strftime("%Y%m%d") in session.expirations)
        build_entry_plan(direction, event.spx.price, funds,
                         [OptionQuote(x.contract.con_id, x.contract.strike, x.ask)
                          for x in event.options])

    def _session(self, row):
        session = ReplaySession.from_dict(json.loads(row[2]))
        if session.session_id != row[0] or session.direction != row[1]:
            raise LocalServiceError("Persisted request and replay input disagree")
        return session

    def _progress(self, row, stop_index=None):
        runner = DurableReplayRunner(self._session(row), self.executor_path)
        try:
            result = runner.run(stop_index=stop_index)
            phase = runner.controller.status().phase
            plan = runner.plan
            journal = runner.controller.journal()
            finished = (result["last_complete_index"] == len(runner.session.events) - 1
                        and phase == "FLAT")
            if finished:
                self.db.execute("UPDATE requests SET completed=1 WHERE request_id=?", (row[0],))
            return result, phase, plan, journal
        finally:
            runner.close()

    def _state(self):
        active = self._active()
        plan = None
        journal = []
        replay = None
        if active:
            # A request committed before process death is resumed from its
            # durable replay cursor; no second intent is invented.
            session = self._session(active)
            runner = DurableReplayRunner(session, self.executor_path)
            try:
                if runner._progress()[3] < session.intent_sequence:
                    runner.run(stop_index=session.intent_sequence)
                replay = runner.result()
                status = runner.controller.status()
                plan = runner.plan
                journal = runner.controller.journal()
                if (replay["last_complete_index"] == len(session.events) - 1
                        and status.phase == "FLAT"):
                    self.db.execute("UPDATE requests SET completed=1 WHERE request_id=?", (active[0],))
                    active = None
            finally:
                runner.close()
        if not active:
            controller = self._controller()
            if controller is None:
                status = None
            else:
                try:
                    status = controller.status()
                    journal = controller.journal()
                finally:
                    controller.close()
        phase = status.phase if status else "FLAT"
        count = status.trades_today if status else 0
        reason = None
        ready = False
        if phase != "FLAT":
            reason = "A simulated lifecycle is active"
        elif count >= 2:
            reason = "Daily limit reached"
        elif active:
            reason = "Replay requires recovery"
        else:
            self._verify_previous_completion()
            if status and status.trading_day > BASE.date().isoformat():
                raise LocalServiceError("Replay clock would move backward")
            try:
                self._evidence_ready("CALL", count)
                self._evidence_ready("PUT", count)
                ready = True
            except Exception:
                reason = "Synthetic account, contract, or market evidence unavailable"
        replay_ended_active = bool(
            active and replay and replay["last_complete_index"] == len(self._session(active).events) - 1
            and phase in {"ENTERING", "OPEN", "EXITING"})
        if replay_ended_active:
            reason = "REPLAY ENDED WITH ACTIVE POSITION"
        view = "READY" if ready else (
            "RECOVERY REQUIRED" if replay_ended_active
            else phase if phase in {"ENTERING", "OPEN", "EXITING"}
            else "BLOCKED" if count >= 2
            else "CONFIRMED FLAT" if phase == "FLAT" and count > 0 and not active
            else "NOT READY")
        if phase == "FLAT" and count > 0 and count < 2 and ready:
            view = "CONFIRMED FLAT"
        activity = [{"type": kind, "detail": self._safe_detail(kind, detail)}
                    for _, kind, detail in journal[-12:]]
        position = None
        if status and status.owned:
            highs = [json.loads(detail)["favorable"] for eid, kind, detail in journal
                     if kind == "HIGH_WATER" and active
                     and eid.startswith("peak:replay:" + active[0] + ":")]
            position = {"direction": status.direction, "quantity": status.owned,
                        "strike": plan["strike"] if plan else None,
                        "simulated_entry_ask": plan["ask"] if plan else None,
                        "entry_spx": plan["spx_price"] if plan else None,
                        "high_water_spx": status.peak,
                        "maximum_favorable_points": highs[-1] if highs else "0",
                        "strategy": "LET_IT_RIDE" if status.ride else
                                    "PROFIT_PROTECTION" if status.profit_protection
                                    else "INITIAL_STOP",
                        "fixed_profit_floor_points": "1.25" if status.profit_protection else None,
                        "exit_reason": status.exit_reason}
        return {"mode": "DRY RUN / SIMULATION", "state": view,
                "lifecycle": phase, "ready": ready, "reason": reason,
                "trade_count": count, "trade_limit": 2,
                "position": position, "activity": activity,
                "development_status": "REPLAY ENDED WITH ACTIVE POSITION" if replay_ended_active else None,
                "fixture": self._fixture(), "fixtures": sorted(PATHS),
                "replay_index": replay["last_complete_index"] if replay else None,
                "replay_total": len(self._session(active).events) if active else None}

    @staticmethod
    def _safe_detail(kind, raw):
        data = json.loads(raw)
        allowed = {"reason", "quantity", "daily_count", "position", "phase", "count", "peak", "favorable"}
        return {key: value for key, value in data.items() if key in allowed}

    def status(self):
        with self.lock:
            try:
                return self._state()
            except Exception:
                return {"mode": "DRY RUN / SIMULATION", "state": "RECOVERY REQUIRED",
                        "lifecycle": "UNKNOWN", "ready": False,
                        "reason": "Persisted state or evidence cannot be verified",
                        "trade_count": None, "trade_limit": 2, "position": None,
                        "activity": [], "development_status": "RECOVERY REQUIRED",
                        "fixture": None, "fixtures": sorted(PATHS),
                        "replay_index": None, "replay_total": None}

    @contextmanager
    def _intent_guard(self):
        # An intent that arrived during an active replay must not wait until
        # that replay becomes flat and then consume the next daily trade.
        if not self.lock.acquire(blocking=False):
            raise LocalServiceError("Another dry-run action is processing",
                                    code="ACTION_IN_PROGRESS", retryable=True)
        try:
            yield
        finally:
            self.lock.release()

    def submit(self, direction, request_id):
        if direction not in {"CALL", "PUT"} or not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise LocalServiceError("Malformed direction or request ID")
        with self._intent_guard():
            old = self.db.execute("SELECT request_id,direction,session_json FROM requests WHERE request_id=?",
                                  (request_id,)).fetchone()
            if old:
                if old[1] != direction:
                    raise LocalServiceError("Request ID already bound to another direction")
                return {"result": "DUPLICATE", "status": self.status()}
            state = self._state()
            if not state["ready"]:
                raise LocalServiceError(state["reason"] or "New simulated entry blocked")
            raw = self._raw(request_id, direction, state["trade_count"])
            session = ReplaySession.from_dict(raw)
            if session.intent_sequence != 0:
                raise LocalServiceError("Demo intent must start at first observation")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self._active() or self.db.execute("SELECT 1 FROM requests WHERE request_id=?", (request_id,)).fetchone():
                    raise LocalServiceError("Intent race blocked")
                self.db.execute("INSERT INTO requests VALUES (?,?,?,0)",
                                (request_id, direction, json.dumps(raw, sort_keys=True)))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            self._progress((request_id, direction, json.dumps(raw)), stop_index=0)
            return {"result": "ACCEPTED", "status": self.status()}

    def load_fixture(self, name):
        if not isinstance(name, str) or name not in PATHS:
            raise LocalServiceError("Unknown canonical fixture")
        with self.lock:
            state = self._state()
            if self._active() or state["lifecycle"] != "FLAT":
                raise LocalServiceError("Cannot change fixture during a lifecycle")
            self.db.execute("UPDATE settings SET fixture=? WHERE singleton=1", (name,))
            return self.status()

    def advance(self, all_remaining=False):
        with self.lock:
            row = self._active()
            if row is None:
                state = self._state()
                if state["ready"]:
                    raise LocalServiceError(
                        "Run arrived before an intent was accepted; retry after CALLS or PUTS completes",
                        code="INTENT_NOT_READY_RETRY", retryable=True)
                raise LocalServiceError("No active synthetic replay", code="NO_ACTIVE_REPLAY")
            session = self._session(row)
            runner = DurableReplayRunner(session, self.executor_path)
            try:
                cursor = runner._progress()[3]
            finally:
                runner.close()
            if cursor >= len(session.events) - 1:
                raise LocalServiceError("REPLAY ENDED WITH ACTIVE POSITION",
                                        code="REPLAY_ENDED_ACTIVE_POSITION")
            stop = len(session.events) - 1 if all_remaining else cursor + 1
            result, phase, _, _ = self._progress(row, stop_index=stop)
            if result["last_complete_index"] == len(session.events) - 1 and phase != "FLAT":
                raise LocalServiceError("REPLAY ENDED WITH ACTIVE POSITION",
                                        code="REPLAY_ENDED_ACTIVE_POSITION")
            return self.status()
