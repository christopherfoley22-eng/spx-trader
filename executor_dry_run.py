"""Offline Executor lifecycle. No IBKR import, connection, or order method.

The simulator ledger and controller state share one durable SQLite transaction.
Market and account evidence are supplied by a synthetic or read-only adapter.
"""

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from mortificatio_v01 import MortificatioV01Error, OptionQuote, build_entry_plan, exit_chunks
from simulation_evidence import (
    EvidenceClock, EvidenceError, SPXObservation, OptionObservation,
    VerifiedAccountSnapshot, VerifiedBrokerSnapshot, VerifiedOptionContract,
    conservative_usable_funds, positive_number, validate_broker_snapshot,
)

NY = ZoneInfo("America/New_York")
MAX_TRADES = 2
PROFIT_PROTECTION_ARM = Decimal("4.00")
PROFIT_PROTECTION_FLOOR = Decimal("1.25")
LET_IT_RIDE_ARM = Decimal("5.00")


class DryRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class DryRunStatus:
    phase: str
    direction: Optional[str]
    planned: int
    owned: int
    con_id: Optional[int]
    trades_today: int
    trading_day: str
    ride: bool
    profit_protection: bool
    peak: str
    exit_reason: Optional[str]


class DryRunExecutor:
    """Single-writer, event-idempotent simulated lifecycle.

    ``press`` is the sole user action. Every later method consumes external
    evidence or simulated fill callbacks, and never invokes a broker API.
    """

    def __init__(self, path, selected_account, session_has_expiration,
                 max_exit_chunk=10, initial_trading_day=None):
        if not isinstance(selected_account, str) or not selected_account.strip():
            raise DryRunError("Explicit runtime account required")
        if not callable(session_has_expiration):
            raise DryRunError("Expiration calendar required")
        if max_exit_chunk not in (5, 10):
            raise DryRunError("Exit chunk size must be 5 or 10")
        if initial_trading_day is not None:
            try:
                if date.fromisoformat(initial_trading_day).isoformat() != initial_trading_day:
                    raise ValueError
            except (TypeError, ValueError):
                raise DryRunError("Initial replay trading date invalid")
        self.selected_account = selected_account
        self.session_has_expiration = session_has_expiration
        self.max_exit_chunk = max_exit_chunk
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS controller (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                account_hash TEXT NOT NULL, phase TEXT NOT NULL,
                intent_id TEXT, direction TEXT, plan TEXT,
                trading_day TEXT NOT NULL, trades_today INTEGER NOT NULL,
                broker_qty INTEGER NOT NULL, broker_con_id INTEGER,
                entry_spx TEXT, peak TEXT, ride INTEGER NOT NULL,
                exit_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY, side TEXT NOT NULL,
                capacity INTEGER NOT NULL, filled INTEGER NOT NULL DEFAULT 0,
                terminal INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS audit (
                event_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_watermark (
                key TEXT PRIMARY KEY, source_time TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS completed_trades (
                intent_id TEXT PRIMARY KEY, trading_day TEXT NOT NULL,
                direction TEXT NOT NULL, con_id INTEGER NOT NULL,
                filled_quantity INTEGER NOT NULL, exit_reason TEXT NOT NULL,
                authorized_plan TEXT NOT NULL
            );
        """)
        account_hash = hashlib.sha256(selected_account.encode()).hexdigest()
        row = self.db.execute("SELECT account_hash FROM controller WHERE singleton=1").fetchone()
        if row is None:
            self.db.execute("INSERT INTO controller VALUES (1, ?, 'FLAT', NULL, NULL, NULL, ?, 0, 0, NULL, NULL, NULL, 0, NULL)",
                            (account_hash, initial_trading_day or datetime.now(NY).date().isoformat()))
        elif row[0] != account_hash:
            raise DryRunError("Runtime account differs from persisted binding")
        self._check_state()

    def close(self):
        self.db.close()

    def _row(self):
        return self.db.execute("SELECT phase,intent_id,direction,plan,trading_day,trades_today,broker_qty,broker_con_id,entry_spx,peak,ride,exit_reason FROM controller WHERE singleton=1").fetchone()

    def _check_state(self):
        row = self._row()
        if row is None:
            raise DryRunError("Controller state missing")
        phase, intent, direction, raw_plan, day, count, qty, con_id, entry, peak, ride, reason = row
        if phase not in {"FLAT", "INTENT", "ENTERING", "OPEN", "EXITING"}:
            raise DryRunError("Unknown controller phase")
        if not isinstance(count, int) or not 0 <= count <= MAX_TRADES or not isinstance(qty, int) or not 0 <= qty <= 25:
            raise DryRunError("Corrupted count or broker quantity")
        try:
            if datetime.fromisoformat(day).date().isoformat() != day:
                raise ValueError
        except (TypeError, ValueError):
            raise DryRunError("Corrupted trading day")
        if (qty == 0) != (con_id is None):
            raise DryRunError("Contradictory simulated broker position")
        opened_today = 0
        for (detail,) in self.db.execute("SELECT detail FROM audit WHERE kind='TRADE_COUNTED'"):
            try:
                if json.loads(detail)["day"] == day:
                    opened_today += 1
            except (TypeError, ValueError, KeyError):
                raise DryRunError("Corrupted trade-count audit")
        if opened_today != count:
            raise DryRunError("Daily count disagrees with durable fills")
        if phase == "FLAT" and (intent is not None or direction is not None or raw_plan is not None or qty or entry is not None or peak is not None or ride or reason):
            raise DryRunError("Contradictory flat state")
        if phase != "FLAT" and (not intent or direction not in {"CALL", "PUT"}):
            raise DryRunError("Active state missing intent")
        if phase == "INTENT" and (raw_plan is not None or qty or entry is not None
                                  or peak is not None or ride or reason):
            raise DryRunError("Intent contains plan or position")
        if phase in {"ENTERING", "OPEN", "EXITING"}:
            plan = self._plan(raw_plan)
            if (plan["direction"] != direction or not isinstance(plan["quantity"], int)
                    or isinstance(plan["quantity"], bool) or not 1 <= plan["quantity"] <= 25):
                raise DryRunError("Plan contradicts lifecycle")
            try:
                contract = VerifiedOptionContract(**plan["contract"])
                if (contract.con_id != plan["con_id"] or contract.strike != plan["strike"]
                        or contract.right != {"CALL": "C", "PUT": "P"}[direction]
                        or contract.expiration != day.replace("-", "")
                        or contract.symbol != "SPX" or contract.sec_type != "OPT"
                        or contract.trading_class != "SPXW" or contract.multiplier != "100"
                        or contract.currency != "USD" or not isinstance(plan["chunks"], list)
                        or not positive_number(contract.strike)
                        or not positive_number(plan["ask"]) or not positive_number(plan["bid"])
                        or plan["bid"] > plan["ask"] or not positive_number(plan["spx_price"])
                        or any(not isinstance(x, int) or isinstance(x, bool)
                               or not 1 <= x <= 10 for x in plan["chunks"])
                        or sum(plan["chunks"]) != plan["quantity"]):
                    raise DryRunError("Persisted contract or chunks contradict plan")
            except (TypeError, KeyError):
                raise DryRunError("Persisted contract metadata corrupted")
            audit_plan = self.db.execute("SELECT detail FROM audit WHERE event_id=? AND kind='PLAN_AUTHORIZED'",
                                         ("plan:" + intent,)).fetchone()
            if audit_plan is None:
                raise DryRunError("Authorized plan audit missing")
            try:
                evidence = json.loads(audit_plan[0])
                if (evidence["contract"] != plan["contract"]
                        or evidence["ask"] != plan["ask"] or evidence["bid"] != plan["bid"]
                        or evidence["quantity"] != plan["quantity"]
                        or evidence["chunks"] != plan["chunks"]
                        or evidence["spx"] != plan["spx_price"]):
                    raise DryRunError("Plan differs from authorization audit")
            except (TypeError, ValueError, KeyError):
                raise DryRunError("Authorization audit corrupted")
            if qty and con_id != plan["con_id"]:
                raise DryRunError("Broker contract contradicts plan")
        if phase == "OPEN" and (qty == 0 or entry is None or peak is None or reason):
            raise DryRunError("Open state incomplete")
        if phase == "EXITING" and (entry is None or peak is None or reason is None):
            raise DryRunError("Exit state incomplete")
        if ride not in (0, 1) or isinstance(ride, bool):
            raise DryRunError("Strategy mode corrupted")
        if phase in {"OPEN", "EXITING"}:
            try:
                entry_value, peak_value = Decimal(entry), Decimal(peak)
                favorable_peak = ((peak_value-entry_value) if direction == "CALL"
                                  else (entry_value-peak_value))
                if (not entry_value.is_finite() or not peak_value.is_finite()
                        or entry_value <= 0 or peak_value <= 0 or favorable_peak < 0
                        or bool(ride) != (favorable_peak >= LET_IT_RIDE_ARM)):
                    raise DryRunError("Strategy high-water state corrupted")
                armed = self.db.execute(
                    "SELECT detail FROM audit WHERE event_id=? AND kind='PROFIT_PROTECTION_ARMED'",
                    ("profit:" + intent,)).fetchone()
                if (favorable_peak >= PROFIT_PROTECTION_ARM) != (armed is not None):
                    raise DryRunError("Profit-protection state disagrees with durable high-water")
                if armed is not None and json.loads(armed[0]) != {"floor": "1.25"}:
                    raise DryRunError("Profit-protection floor audit corrupted")
            except (TypeError, ValueError):
                raise DryRunError("Strategy price state corrupted")
            if phase == "EXITING" and reason not in {"INITIAL_STOP", "PROFIT_PROTECTION_FLOOR",
                                                        "LET_IT_RIDE_REVERSAL"}:
                raise DryRunError("Unknown exit reason")
        return row

    @staticmethod
    def _plan(raw):
        try:
            plan = json.loads(raw)
            if (not isinstance(plan["con_id"], int) or isinstance(plan["con_id"], bool)
                    or plan["con_id"] <= 0):
                raise ValueError
            return plan
        except (TypeError, ValueError, KeyError):
            raise DryRunError("Authorized plan corrupted")

    def status(self):
        phase, _, direction, raw, day, count, qty, con_id, entry, peak, ride, reason = self._check_state()
        protected = (phase in {"OPEN", "EXITING"} and not ride
                     and ((Decimal(peak) - Decimal(entry)) if direction == "CALL"
                          else (Decimal(entry) - Decimal(peak))) >= PROFIT_PROTECTION_ARM)
        return DryRunStatus(phase, direction, self._plan(raw)["quantity"] if raw else 0,
                            qty, con_id, count, day, bool(ride), protected, peak or "0", reason)

    def journal(self):
        return self.db.execute("SELECT event_id,kind,detail FROM audit ORDER BY rowid").fetchall()

    def completed_trades(self):
        return self.db.execute("SELECT intent_id,trading_day,direction,con_id,filled_quantity,exit_reason,authorized_plan FROM completed_trades ORDER BY rowid").fetchall()

    def _event(self, event_id, kind, detail):
        if not isinstance(event_id, str) or not event_id or len(event_id) > 160:
            raise DryRunError("Stable event ID required")
        payload = json.dumps(detail, sort_keys=True, separators=(",", ":"))
        old = self.db.execute("SELECT kind,detail FROM audit WHERE event_id=?", (event_id,)).fetchone()
        if old:
            if old != (kind, payload):
                raise DryRunError("Event ID replay has different meaning")
            return False
        self.db.execute("INSERT INTO audit VALUES (?,?,?,?)",
                        (event_id, kind, payload, datetime.now(NY).isoformat()))
        return True

    def _transaction(self, action):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = action()
            self._check_state()
            self.db.execute("COMMIT")
            return result
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _reconcile(self, snapshot, now_monotonic):
        validate_broker_snapshot(snapshot, self.selected_account, now_monotonic)
        row = self._check_state()
        if snapshot.position_qty != row[6] or snapshot.con_id != row[7] or snapshot.open_order_count:
            raise DryRunError("Broker evidence disagrees with simulator")
        return row

    def _roll_day(self, day):
        row = self._check_state()
        if day < row[4]:
            raise DryRunError("New York date moved backward")
        if day > row[4]:
            if row[0] != "FLAT":
                raise DryRunError("Active lifecycle crossed trading date")
            self.db.execute("UPDATE controller SET trading_day=?,trades_today=0 WHERE singleton=1", (day,))
            self._event("day:" + day, "DAY_ROLLOVER", {"day": day, "count": 0})

    def press(self, intent_id, direction, snapshot, now_wall, now_monotonic):
        """Accept one CALLS/PUTS human choice; no SELL user action exists."""
        if (not isinstance(intent_id, str) or not intent_id or len(intent_id) > 120
                or direction not in {"CALL", "PUT"} or not isinstance(now_wall, datetime)
                or now_wall.tzinfo is None):
            raise DryRunError("Valid direction and New York timestamp required")
        day = now_wall.astimezone(NY).date().isoformat()
        def action():
            row = self._reconcile(snapshot, now_monotonic)
            if row[1] == intent_id and row[2] == direction:
                return "DUPLICATE"
            self._event("reconcile:intent:" + intent_id, "RECONCILED",
                        {"phase": row[0], "position": row[6], "open_orders": 0})
            self._roll_day(day)
            row = self._check_state()
            if row[0] != "FLAT" or row[5] >= MAX_TRADES:
                self._event("reject:" + str(intent_id), "INTENT_REJECTED",
                            {"reason": "ACTIVE" if row[0] != "FLAT" else "DAILY_LIMIT"})
                return "REJECTED"
            if self.db.execute("SELECT 1 FROM audit WHERE event_id=?", ("intent:" + str(intent_id),)).fetchone():
                raise DryRunError("Completed intent ID cannot be reused")
            self._event("intent:" + str(intent_id), "INTENT_ACCEPTED", {"direction": direction, "day": day})
            self.db.execute("UPDATE controller SET phase='INTENT',intent_id=?,direction=? WHERE singleton=1", (intent_id, direction))
            return "ACCEPTED"
        return self._transaction(action)

    def _market(self, direction, spx, options, now_wall, now_monotonic):
        if not options:
            raise EvidenceError("No option observations")
        clock = EvidenceClock()
        for key, source in self.db.execute("SELECT key,source_time FROM market_watermark"):
            clock.last_source[("OPTION", int(key[7:])) if key.startswith("OPTION:") else key] = datetime.fromisoformat(source)
        for option in options:
            clock.validate_pair(direction, spx, option, now_wall, now_monotonic,
                                self.session_has_expiration)
        if len({o.contract.con_id for o in options}) != len(options):
            raise EvidenceError("Duplicate option contract")
        for key, source in clock.last_source.items():
            name = "OPTION:" + str(key[1]) if isinstance(key, tuple) else key
            self.db.execute("INSERT INTO market_watermark VALUES (?,?) ON CONFLICT(key) DO UPDATE SET source_time=excluded.source_time",
                            (name, source.isoformat()))

    def authorize(self, intent_id, account: VerifiedAccountSnapshot,
                  spx: SPXObservation, options: Sequence[OptionObservation],
                  snapshot: VerifiedBrokerSnapshot, now_wall, now_monotonic):
        def action():
            row = self._reconcile(snapshot, now_monotonic)
            if row[0] != "INTENT" or row[1] != intent_id or row[4] != now_wall.astimezone(NY).date().isoformat():
                raise DryRunError("Intent or trading date changed")
            funds = conservative_usable_funds(account, self.selected_account, now_monotonic)
            self._market(row[2], spx, options, now_wall, now_monotonic)
            self._event("evidence:" + intent_id, "EVIDENCE_VALIDATED",
                        {"account": "SELECTED_FRESH_USD", "spx": "LIVE_FRESH",
                         "option_quotes": len(options), "expiration": "SESSION_VERIFIED"})
            plan = build_entry_plan(row[2], spx.price, funds,
                                    [OptionQuote(o.contract.con_id, o.contract.strike, o.ask) for o in options])
            chosen = next(o for o in options if o.contract.con_id == plan.con_id)
            payload = {"direction": plan.direction, "spx_price": plan.spx_price,
                       "con_id": plan.con_id, "strike": plan.strike, "ask": plan.ask,
                       "bid": chosen.bid, "quantity": plan.quantity,
                       "contract": asdict(chosen.contract), "chunks": plan.chunks}
            self.db.execute("UPDATE controller SET phase='ENTERING',plan=? WHERE singleton=1",
                            (json.dumps(payload, sort_keys=True),))
            for index, size in enumerate(plan.chunks):
                chunk_id = str(intent_id) + ":entry:" + str(index)
                self.db.execute("INSERT INTO chunks(chunk_id,side,capacity) VALUES (?, 'ENTRY', ?)",
                                (chunk_id, size))
                self._event("chunk:" + chunk_id, "ENTRY_CHUNK_PLANNED", {"quantity": size})
            self._event("plan:" + str(intent_id), "PLAN_AUTHORIZED",
                        {"contract": payload["contract"], "bid": chosen.bid, "ask": plan.ask,
                         "cost_per_contract": str(Decimal(str(plan.ask)) * 100),
                         "affordable_contracts_capped": plan.quantity,
                         "quantity": plan.quantity,
                         "chunks": plan.chunks, "spx": spx.price})
            return payload
        try:
            return self._transaction(action)
        except (EvidenceError, MortificatioV01Error) as exc:
            # Failed evidence cannot authorize a plan, but its reason remains
            # visible in the durable audit for the same pending intent.
            def record_failure():
                row = self._check_state()
                if row[0] == "INTENT" and row[1] == intent_id:
                    prefix = "evidence-rejected:" + intent_id + ":"
                    count = self.db.execute("SELECT count(*) FROM audit WHERE kind='EVIDENCE_REJECTED' AND substr(event_id,1,length(?))=?",
                                            (prefix, prefix)).fetchone()[0]
                    self._event("evidence-rejected:" + intent_id + ":" + str(count),
                                "EVIDENCE_REJECTED", {"reason": str(exc)})
            self._transaction(record_failure)
            raise

    def entry_chunks(self):
        if self.status().phase != "ENTERING":
            raise DryRunError("No entry in progress")
        return self.db.execute("SELECT chunk_id,capacity-filled FROM chunks WHERE side='ENTRY' AND terminal=0 ORDER BY rowid").fetchall()

    def entry_event(self, event_id, chunk_id, fill_qty=0, rejected=False,
                    fill_con_id=None,
                    simulated_price=None,
                    account=None, spx=None, option=None, snapshot=None,
                    now_wall=None, now_monotonic=None):
        def action():
            row = self._check_state()
            old = self.db.execute("SELECT kind,detail FROM audit WHERE event_id=?", (event_id,)).fetchone()
            if simulated_price is not None:
                try:
                    price = Decimal(str(simulated_price))
                except (TypeError, ValueError, InvalidOperation):
                    raise DryRunError("Simulated entry price malformed")
                if not price.is_finite() or price <= 0:
                    raise DryRunError("Simulated entry price invalid")
                simulated_price_text = str(price)
            else:
                simulated_price_text = None
            detail = {"chunk": chunk_id, "fill": fill_qty, "rejected": rejected,
                      "con_id": fill_con_id, "simulated_price": simulated_price_text}
            if old:
                if old != ("ENTRY_EVENT", json.dumps(detail, sort_keys=True, separators=(",", ":"))):
                    raise DryRunError("Changed fill event replay")
                return "DUPLICATE"
            if row[0] != "ENTERING":
                raise DryRunError("Entry fill outside ENTERING")
            if snapshot is None or now_monotonic is None:
                raise DryRunError("Broker reconciliation required before entry fill")
            self._reconcile(snapshot, now_monotonic)
            chunk = self.db.execute("SELECT capacity,filled,terminal FROM chunks WHERE chunk_id=? AND side='ENTRY'", (chunk_id,)).fetchone()
            first = self.db.execute("SELECT chunk_id FROM chunks WHERE side='ENTRY' AND terminal=0 ORDER BY rowid LIMIT 1").fetchone()
            if first is None or first[0] != chunk_id:
                raise DryRunError("Entry chunk arrived out of order")
            if chunk is None or chunk[2] or not isinstance(fill_qty, int) or isinstance(fill_qty, bool) or fill_qty < 0 or fill_qty > chunk[0]-chunk[1]:
                raise DryRunError("Invalid entry chunk fill")
            if fill_qty == 0 and not rejected:
                raise DryRunError("Empty fill must be rejected")
            if fill_qty and (not isinstance(fill_con_id, int) or isinstance(fill_con_id, bool)
                             or fill_con_id != self._plan(row[3])["con_id"]):
                raise DryRunError("Entry fill contract differs from authorization")
            if fill_qty:
                if not isinstance(option, OptionObservation) or not isinstance(spx, SPXObservation):
                    raise EvidenceError("Fresh exact option and SPX evidence required for entry fill")
                funds = conservative_usable_funds(account, self.selected_account, now_monotonic)
                self._market(row[2], spx, [option], now_wall, now_monotonic)
                plan = self._plan(row[3])
                if (asdict(option.contract) != plan["contract"] or option.quote_con_id != plan["con_id"]
                        or option.bid != plan["bid"] or option.ask != plan["ask"]
                        or spx.price != plan["spx_price"]
                        or (simulated_price_text is not None
                            and price != Decimal(str(option.ask)))
                        or funds < Decimal(str(row[6] + fill_qty)) * Decimal(str(plan["ask"])) * 100):
                    raise EvidenceError("Entry evidence changed after authorization")
            self._event(event_id, "ENTRY_EVENT", detail)
            self.db.execute("UPDATE chunks SET filled=filled+?,terminal=? WHERE chunk_id=?",
                            (fill_qty, int(rejected or chunk[1]+fill_qty == chunk[0]), chunk_id))
            new_qty = row[6] + fill_qty
            if new_qty > self._plan(row[3])["quantity"]:
                raise DryRunError("Entry would exceed authorization")
            self.db.execute("UPDATE controller SET broker_qty=?,broker_con_id=? WHERE singleton=1",
                            (new_qty, self._plan(row[3])["con_id"] if new_qty else None))
            if fill_qty and row[6] == 0:
                if row[5] >= MAX_TRADES:
                    raise DryRunError("Daily trade cap reached before first fill")
                self.db.execute("UPDATE controller SET trades_today=trades_today+1 WHERE singleton=1")
                self._event("count:" + row[1], "TRADE_COUNTED",
                            {"day": row[4], "daily_count": row[5]+1,
                             "first_fill_event": event_id})
            return "FILLED" if fill_qty else "REJECTED"
        return self._transaction(action)

    def finish_entry(self, snapshot, now_monotonic):
        def action():
            row = self._reconcile(snapshot, now_monotonic)
            if row[0] != "ENTERING":
                raise DryRunError("No entry to finish")
            if self.db.execute("SELECT 1 FROM chunks WHERE side='ENTRY' AND terminal=0 LIMIT 1").fetchone():
                raise DryRunError("Entry chunks unresolved")
            self._event("reconcile:open:" + row[1], "RECONCILED",
                        {"phase": "ENTERING", "position": row[6], "open_orders": 0})
            if row[6] == 0:
                self.db.execute("UPDATE controller SET phase='FLAT',intent_id=NULL,direction=NULL,plan=NULL WHERE singleton=1")
                self._event("entry-empty:" + row[1], "ENTRY_REJECTED", {"count": row[5]})
                self.db.execute("DELETE FROM chunks")
                return "FLAT"
            if self.db.execute("SELECT 1 FROM audit WHERE event_id=? AND kind='TRADE_COUNTED'",
                               ("count:" + row[1],)).fetchone() is None:
                raise DryRunError("Filled entry has no durable daily count")
            plan = self._plan(row[3])
            self.db.execute("UPDATE controller SET phase='OPEN',entry_spx=?,peak=? WHERE singleton=1",
                            (str(plan["spx_price"]), str(plan["spx_price"])))
            self._event("open:" + row[1], "OPEN_CONFIRMED",
                        {"quantity": row[6], "con_id": row[7], "daily_count": row[5],
                         "day": row[4]})
            return "OPEN"
        return self._transaction(action)

    def observe(self, event_id, spx: SPXObservation, snapshot,
                now_wall, now_monotonic):
        def action():
            row = self._reconcile(snapshot, now_monotonic)
            old_event = self.db.execute("SELECT kind,detail FROM audit WHERE event_id=?", (event_id,)).fetchone()
            if old_event:
                prior = json.loads(old_event[1])
                if (old_event[0] != "MARKET_OBSERVED" or prior.get("price") != spx.price
                        or prior.get("source_time") != spx.source_time.isoformat()
                        or prior.get("status") != spx.status.value
                        or prior.get("receipt") != spx.received_monotonic):
                    raise DryRunError("Changed market event replay")
                return self.status().phase
            if row[0] not in {"OPEN", "EXITING"}:
                raise DryRunError("No open position to monitor")
            self._event("reconcile:market:" + event_id, "RECONCILED",
                        {"phase": row[0], "position": row[6], "open_orders": 0})
            clock = EvidenceClock()
            old = self.db.execute("SELECT source_time FROM market_watermark WHERE key='SPX'").fetchone()
            if old:
                clock.last_source["SPX"] = datetime.fromisoformat(old[0])
            clock.validate_spx(spx, now_wall, now_monotonic)
            self.db.execute("INSERT INTO market_watermark VALUES ('SPX',?) ON CONFLICT(key) DO UPDATE SET source_time=excluded.source_time",
                            (spx.source_time.isoformat(),))
            if row[0] == "EXITING":
                self._event(event_id, "MARKET_OBSERVED", {"phase": "EXITING",
                                                           "price": spx.price,
                                                           "source_time": spx.source_time.isoformat(),
                                                           "status": spx.status.value,
                                                           "receipt": spx.received_monotonic})
                return "EXITING"
            price = Decimal(str(spx.price))
            entry = Decimal(row[8])
            previous_peak = Decimal(row[9])
            favorable = (price-entry) if row[2] == "CALL" else (entry-price)
            best = (previous_peak-entry) if row[2] == "CALL" else (entry-previous_peak)
            if favorable > best:
                best = favorable
                self.db.execute("UPDATE controller SET peak=? WHERE singleton=1", (str(price),))
                self._event("peak:" + event_id, "HIGH_WATER", {"favorable": str(best)})
                if best >= PROFIT_PROTECTION_ARM:
                    self._event("profit:" + row[1], "PROFIT_PROTECTION_ARMED",
                                {"floor": "1.25"})
            ride = bool(row[10]) or best >= LET_IT_RIDE_ARM
            if ride and not row[10]:
                self.db.execute("UPDATE controller SET ride=1 WHERE singleton=1")
                self._event("ride:" + row[1], "LET_IT_RIDE", {"peak": str(best)})
            reversal = best-favorable
            reason = None
            if ride:
                if reversal >= Decimal("3.00"):
                    reason = "LET_IT_RIDE_REVERSAL"
            elif best >= PROFIT_PROTECTION_ARM and favorable <= PROFIT_PROTECTION_FLOOR:
                reason = "PROFIT_PROTECTION_FLOOR"
            elif favorable <= Decimal("-3.25"):
                reason = "INITIAL_STOP"
            if reason:
                self.db.execute("UPDATE controller SET phase='EXITING',exit_reason=? WHERE singleton=1", (reason,))
                self._event("exit:" + row[1], "EXIT_TRIGGERED",
                            {"reason": reason, "favorable": str(favorable),
                             "peak": str(best), "reversal": str(reversal)})
            self._event(event_id, "MARKET_OBSERVED", {"phase": "EXITING" if reason else "OPEN",
                                                       "price": spx.price,
                                                       "source_time": spx.source_time.isoformat(),
                                                       "status": spx.status.value,
                                                       "receipt": spx.received_monotonic})
            return "EXITING" if reason else "OPEN"
        return self._transaction(action)

    def next_exit_chunk(self, snapshot, now_monotonic):
        def action():
            row = self._reconcile(snapshot, now_monotonic)
            if row[0] != "EXITING" or row[6] == 0:
                return None
            pending = self.db.execute("SELECT chunk_id,capacity-filled FROM chunks WHERE side='EXIT' AND terminal=0").fetchall()
            if len(pending) > 1:
                raise DryRunError("Multiple unresolved exit chunks")
            if pending:
                return pending[0]
            size = exit_chunks(row[6], self.max_exit_chunk)[0]
            ordinal = self.db.execute("SELECT count(*) FROM chunks WHERE side='EXIT'").fetchone()[0]
            chunk_id = row[1] + ":exit:" + str(ordinal)
            self.db.execute("INSERT INTO chunks(chunk_id,side,capacity) VALUES (?,'EXIT',?)", (chunk_id, size))
            self._event("chunk:" + chunk_id, "EXIT_CHUNK_PLANNED", {"quantity": size})
            return chunk_id, size
        return self._transaction(action)

    def exit_event(self, event_id, chunk_id, fill_qty=0, rejected=False,
                   fill_con_id=None,
                   simulated_price=None,
                   snapshot=None, now_monotonic=None):
        def action():
            row = self._check_state()
            if simulated_price is not None:
                try:
                    price = Decimal(str(simulated_price))
                except (TypeError, ValueError, InvalidOperation):
                    raise DryRunError("Simulated exit price malformed")
                if not price.is_finite() or price <= 0:
                    raise DryRunError("Simulated exit price invalid")
                simulated_price_text = str(price)
            else:
                simulated_price_text = None
            detail = {"chunk": chunk_id, "fill": fill_qty, "rejected": rejected,
                      "con_id": fill_con_id, "simulated_price": simulated_price_text}
            old = self.db.execute("SELECT kind,detail FROM audit WHERE event_id=?", (event_id,)).fetchone()
            if old:
                if old != ("EXIT_EVENT", json.dumps(detail, sort_keys=True, separators=(",", ":"))):
                    raise DryRunError("Changed exit event replay")
                return "DUPLICATE"
            if row[0] != "EXITING" or row[6] <= 0:
                raise DryRunError("No owned position to exit")
            if snapshot is None or now_monotonic is None:
                raise DryRunError("Broker reconciliation required before exit fill")
            self._reconcile(snapshot, now_monotonic)
            chunk = self.db.execute("SELECT capacity,filled,terminal FROM chunks WHERE chunk_id=? AND side='EXIT'", (chunk_id,)).fetchone()
            if chunk is None or chunk[2] or not isinstance(fill_qty, int) or isinstance(fill_qty, bool) or fill_qty < 0 or fill_qty > min(chunk[0]-chunk[1], row[6]):
                raise DryRunError("Exit fill exceeds owned or chunk quantity")
            if fill_qty == 0 and not rejected:
                raise DryRunError("Empty exit fill must be rejected")
            if fill_qty and (not isinstance(fill_con_id, int) or isinstance(fill_con_id, bool)
                             or fill_con_id != row[7]):
                raise DryRunError("Exit fill contract differs from owned position")
            self._event(event_id, "EXIT_EVENT", detail)
            self.db.execute("UPDATE chunks SET filled=filled+?,terminal=? WHERE chunk_id=?",
                            (fill_qty, int(rejected or chunk[1]+fill_qty == chunk[0] or fill_qty == row[6]), chunk_id))
            remaining = row[6]-fill_qty
            self.db.execute("UPDATE controller SET broker_qty=?,broker_con_id=? WHERE singleton=1",
                            (remaining, row[7] if remaining else None))
            return "FILLED" if fill_qty else "REJECTED"
        return self._transaction(action)

    def confirm_flat(self, snapshot, now_monotonic):
        def action():
            row = self._reconcile(snapshot, now_monotonic)
            if row[0] != "EXITING" or row[6] != 0:
                raise DryRunError("Confirmed flat requires exit and zero broker position")
            if self.db.execute("SELECT 1 FROM chunks WHERE side='EXIT' AND terminal=0 LIMIT 1").fetchone():
                raise DryRunError("Unresolved exit chunk")
            self._event("reconcile:flat:" + row[1], "RECONCILED",
                        {"phase": "EXITING", "position": 0, "open_orders": 0})
            opened = self.db.execute("SELECT detail FROM audit WHERE event_id=? AND kind='OPEN_CONFIRMED'",
                                     ("open:" + row[1],)).fetchone()
            if opened is None:
                raise DryRunError("Open confirmation audit missing")
            filled_quantity = json.loads(opened[0])["quantity"]
            self.db.execute("INSERT INTO completed_trades VALUES (?,?,?,?,?,?,?)",
                            (row[1], row[4], row[2], self._plan(row[3])["con_id"],
                             filled_quantity, row[11], row[3]))
            self._event("flat:" + row[1], "CONFIRMED_FLAT",
                        {"reason": row[11], "daily_count": row[5], "day": row[4]})
            self.db.execute("UPDATE controller SET phase='FLAT',intent_id=NULL,direction=NULL,plan=NULL,entry_spx=NULL,peak=NULL,ride=0,exit_reason=NULL WHERE singleton=1")
            self.db.execute("DELETE FROM chunks")
            return "CONFIRMED_FLAT"
        return self._transaction(action)
