"""Versioned OFFLINE replay for DryRunExecutor. No brokerage I/O.

All prices, broker evidence and fills here are synthetic. This module never
implements a strategy: every entry, risk and exit decision goes through the
existing verified dry-run controller.
"""

from dataclasses import asdict, dataclass
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import tempfile
from typing import Optional
from zoneinfo import ZoneInfo

from executor_dry_run import DryRunError, DryRunExecutor
from simulation_evidence import (
    EvidenceClock, EvidenceError, FeedStatus, OptionObservation,
    SPXObservation, VerifiedAccountSnapshot, VerifiedBrokerSnapshot,
    VerifiedOptionContract, validate_broker_snapshot,
)

NY = ZoneInfo("America/New_York")
FORMAT_VERSION = 1


class ReplayFormatError(ValueError):
    pass


def _number(value, label, positive=False):
    try:
        finite = math.isfinite(value) if isinstance(value, (int, float)) else False
    except (OverflowError, TypeError, ValueError):
        finite = False
    if isinstance(value, bool) or not finite:
        raise ReplayFormatError(label + " must be a finite number")
    if positive and value <= 0:
        raise ReplayFormatError(label + " must be positive")
    return value


def _time(value, label):
    if not isinstance(value, str):
        raise ReplayFormatError(label + " must have an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ReplayFormatError(label + " timestamp malformed")
    if parsed.tzinfo is None:
        raise ReplayFormatError(label + " timestamp lacks timezone")
    return parsed


def _status(value):
    try:
        return FeedStatus(value)
    except (TypeError, ValueError):
        raise ReplayFormatError("Unknown market-data status")


def _exact_keys(value, required, optional, label):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ReplayFormatError(label + " fields missing or unknown")


@dataclass(frozen=True)
class ReplayEvent:
    sequence: int
    now_wall: datetime
    monotonic: float
    spx: Optional[SPXObservation]
    options: tuple
    account: Optional[VerifiedAccountSnapshot]
    broker: object
    fills: tuple
    restart: bool


@dataclass(frozen=True)
class ReplaySession:
    session_id: str
    direction: str
    intent_sequence: int
    account_id: str
    expirations: tuple
    fill_mode: str
    max_exit_chunk: int
    events: tuple

    @classmethod
    def from_dict(cls, raw):
        _exact_keys(raw, {"version", "session_id", "direction", "intent_sequence",
                          "account_id", "expirations", "fill_mode", "max_exit_chunk",
                          "events"}, set(), "session")
        if (not isinstance(raw["version"], int) or isinstance(raw["version"], bool)
                or raw["version"] != FORMAT_VERSION):
            raise ReplayFormatError("Unsupported replay version")
        if (not isinstance(raw["session_id"], str) or not raw["session_id"]
                or len(raw["session_id"]) > 100 or not isinstance(raw["direction"], str)
                or raw["direction"] not in {"CALL", "PUT"}
                or not isinstance(raw["account_id"], str) or not raw["account_id"]):
            raise ReplayFormatError("Session identity or direction invalid")
        if (not isinstance(raw["intent_sequence"], int) or isinstance(raw["intent_sequence"], bool)
                or raw["intent_sequence"] < 0 or not isinstance(raw["fill_mode"], str)
                or raw["fill_mode"] not in {"AUTO", "SCRIPTED"}
                or raw["max_exit_chunk"] not in (5, 10)):
            raise ReplayFormatError("Replay settings invalid")
        expirations = raw["expirations"]
        if (not isinstance(expirations, list) or not expirations
                or any(not isinstance(x, str) or len(x) != 8 or not x.isdigit() for x in expirations)
                or len(set(expirations)) != len(expirations)):
            raise ReplayFormatError("Explicit expiration calendar invalid")
        try:
            for value in expirations:
                datetime.strptime(value, "%Y%m%d")
        except ValueError:
            raise ReplayFormatError("Explicit expiration date invalid")
        if not isinstance(raw["events"], list) or not raw["events"] or len(raw["events"]) > 10000:
            raise ReplayFormatError("Replay events missing or excessive")
        events = []
        seen = {}
        last_sequence = -1
        last_wall = None
        last_mono = None
        for item in raw["events"]:
            _exact_keys(item, {"sequence", "ny_time", "monotonic", "spx", "options",
                               "account", "broker", "fills"}, {"restart"}, "event")
            sequence = item["sequence"]
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                raise ReplayFormatError("Replay sequence invalid")
            try:
                canonical = json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError):
                raise ReplayFormatError("Replay event contains malformed or non-finite value")
            if sequence in seen:
                if seen[sequence] != canonical:
                    raise ReplayFormatError("Conflicting duplicate sequence")
                if sequence != last_sequence:
                    raise ReplayFormatError("Nonadjacent duplicate sequence")
                continue
            if sequence <= last_sequence:
                raise ReplayFormatError("Replay sequence moved backward")
            now_wall = _time(item["ny_time"], "New York")
            if now_wall.utcoffset() != now_wall.astimezone(NY).utcoffset():
                raise ReplayFormatError("Timestamp has wrong New York offset")
            mono = float(_number(item["monotonic"], "Replay monotonic"))
            if mono < 0:
                raise ReplayFormatError("Replay monotonic clock negative")
            if (last_wall is not None and now_wall < last_wall) or (last_mono is not None and mono <= last_mono):
                raise ReplayFormatError("Replay clock moved backward")
            spx = _parse_spx(item["spx"], mono)
            options = _parse_options(item["options"], mono)
            account = _parse_account(item["account"], mono)
            broker = _parse_broker(item["broker"], mono)
            fills = _parse_fills(item["fills"])
            restart = item.get("restart", False)
            if not isinstance(restart, bool):
                raise ReplayFormatError("Restart flag invalid")
            events.append(ReplayEvent(sequence, now_wall, mono, spx, options,
                                      account, broker, fills, restart))
            seen[sequence] = canonical
            last_sequence, last_wall, last_mono = sequence, now_wall, mono
        if raw["intent_sequence"] not in seen:
            raise ReplayFormatError("Intent sequence absent")
        if raw["fill_mode"] == "AUTO" and any(event.fills for event in events):
            raise ReplayFormatError("AUTO fill mode cannot ignore scripted fill events")
        return cls(raw["session_id"], raw["direction"], raw["intent_sequence"],
                   raw["account_id"], tuple(expirations), raw["fill_mode"],
                   raw["max_exit_chunk"], tuple(events))

    @classmethod
    def from_json(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text()))


def _parse_spx(raw, mono):
    if raw is None:
        return None
    _exact_keys(raw, {"price", "status", "source_time"}, {"received_monotonic"}, "SPX")
    receipt = float(_number(raw.get("received_monotonic", mono), "SPX receipt"))
    if not 0 <= receipt <= mono:
        raise ReplayFormatError("SPX receipt clock impossible")
    return SPXObservation(_number(raw["price"], "SPX price", True), _status(raw["status"]),
                          _time(raw["source_time"], "SPX source"), receipt)


def _parse_options(raw, mono):
    if not isinstance(raw, list):
        raise ReplayFormatError("Option observations must be a list")
    result = []
    seen = set()
    for item in raw:
        _exact_keys(item, {"contract", "quote_con_id", "bid", "ask", "status",
                           "source_time"}, {"received_monotonic"}, "option")
        _exact_keys(item["contract"], {"symbol", "sec_type", "right", "expiration",
                                       "trading_class", "multiplier", "currency", "strike",
                                       "con_id"}, set(), "contract")
        c = item["contract"]
        if any(not isinstance(c[key], str) for key in ("symbol", "sec_type", "right",
                "expiration", "trading_class", "multiplier", "currency")):
            raise ReplayFormatError("Contract identity malformed")
        if (not isinstance(c["con_id"], int) or isinstance(c["con_id"], bool)
                or c["con_id"] <= 0 or not isinstance(item["quote_con_id"], int)
                or isinstance(item["quote_con_id"], bool) or item["quote_con_id"] <= 0):
            raise ReplayFormatError("Option conId invalid")
        strike = _number(c["strike"], "Option strike", True)
        bid = _number(item["bid"], "Option bid", True)
        ask = _number(item["ask"], "Option ask", True)
        if bid > ask or c["con_id"] in seen:
            raise ReplayFormatError("Crossed quote or duplicate conId")
        seen.add(c["con_id"])
        contract = VerifiedOptionContract(c["symbol"], c["sec_type"], c["right"],
                                          c["expiration"], c["trading_class"],
                                          c["multiplier"], c["currency"], strike, c["con_id"])
        receipt = float(_number(item.get("received_monotonic", mono), "Option receipt"))
        if not 0 <= receipt <= mono:
            raise ReplayFormatError("Option receipt clock impossible")
        result.append(OptionObservation(contract, item["quote_con_id"], bid, ask,
                                        _status(item["status"]),
                                        _time(item["source_time"], "Option source"),
                                        receipt))
    return tuple(result)


def _parse_account(raw, mono):
    if raw is None:
        return None
    fields = {"account_id", "selected_account", "complete", "currency",
              "available_funds", "buying_power", "excess_liquidity",
              "total_cash_value", "settled_cash", "oldest_required_receipt_monotonic"}
    _exact_keys(raw, fields, set(), "account")
    for key in ("available_funds", "buying_power", "excess_liquidity",
                "total_cash_value", "settled_cash"):
        try:
            value = Decimal(str(raw[key]))
        except (TypeError, ValueError, InvalidOperation):
            raise ReplayFormatError("Account numeric evidence malformed")
        if not value.is_finite() or value < 0:
            raise ReplayFormatError("Account numeric evidence invalid")
    receipt = _number(raw["oldest_required_receipt_monotonic"], "Account receipt")
    if not 0 <= receipt <= mono:
        raise ReplayFormatError("Account receipt clock impossible")
    return VerifiedAccountSnapshot(**raw)


def _parse_broker(raw, mono):
    if raw is None:
        return None
    if raw == "SIMULATOR":
        return raw
    fields = {"account_id", "selected_account", "managed_accounts", "complete",
              "position_qty", "con_id", "open_order_count", "received_monotonic"}
    _exact_keys(raw, fields, set(), "broker")
    if not isinstance(raw["managed_accounts"], list):
        raise ReplayFormatError("Broker managed accounts malformed")
    receipt = _number(raw["received_monotonic"], "Broker receipt")
    if not 0 <= receipt <= mono:
        raise ReplayFormatError("Broker receipt clock impossible")
    return VerifiedBrokerSnapshot(raw["account_id"], raw["selected_account"],
                                  tuple(raw["managed_accounts"]), raw["complete"],
                                  raw["position_qty"], raw["con_id"],
                                  raw["open_order_count"], raw["received_monotonic"])


def _parse_fills(raw):
    if not isinstance(raw, list):
        raise ReplayFormatError("Fill events must be a list")
    seen = set()
    result = []
    for item in raw:
        _exact_keys(item, {"event_id", "side", "chunk_index", "quantity", "rejected"},
                    set(), "fill")
        if (not isinstance(item["event_id"], str) or not item["event_id"]
                or item["event_id"] in seen or not isinstance(item["side"], str)
                or item["side"] not in {"ENTRY", "EXIT"}
                or not isinstance(item["chunk_index"], int) or isinstance(item["chunk_index"], bool)
                or item["chunk_index"] < 0 or not isinstance(item["quantity"], int)
                or isinstance(item["quantity"], bool) or not 0 <= item["quantity"] <= 25
                or not isinstance(item["rejected"], bool)
                or (item["quantity"] == 0 and not item["rejected"])):
            raise ReplayFormatError("Simulated fill event malformed")
        seen.add(item["event_id"])
        result.append(item)
    return tuple(result)


class SimulationFillModel:
    """SIMULATION ONLY: BUY at observed ask, SELL at observed bid."""

    def __init__(self):
        self.quantity = 0
        self.con_id = None
        self.position_path = [0]
        self.entry_fills = []
        self.exit_fills = []
        self.buy_total = Decimal(0)
        self.sell_total = Decimal(0)

    def snapshot(self, account_id, monotonic):
        return VerifiedBrokerSnapshot(account_id, account_id, (account_id,), True,
                                      self.quantity, self.con_id, 0, monotonic)

    def filled(self, side, con_id, quantity, price, event_id, chunk_id):
        if quantity == 0:
            return
        unit = Decimal(str(price))
        if not unit.is_finite() or unit <= 0:
            raise ReplayFormatError("Simulated fill price invalid")
        if side == "ENTRY":
            if self.quantity and self.con_id != con_id:
                raise ReplayFormatError("Simulation contract mismatch")
            self.quantity += quantity
            self.con_id = con_id
            self.buy_total += unit * quantity * 100
            self.entry_fills.append({"event_id": event_id, "chunk": chunk_id,
                                     "quantity": quantity, "price": str(unit)})
        else:
            if con_id != self.con_id or quantity > self.quantity:
                raise ReplayFormatError("Simulation exit would oversell")
            self.quantity -= quantity
            if self.quantity == 0:
                self.con_id = None
            self.sell_total += unit * quantity * 100
            self.exit_fills.append({"event_id": event_id, "chunk": chunk_id,
                                    "quantity": quantity, "price": str(unit)})
        self.position_path.append(self.quantity)


class ReplayRunner:
    def __init__(self, session):
        if not isinstance(session, ReplaySession):
            raise ReplayFormatError("Validated replay session required")
        self.session = session

    def run(self, restart_sequences=(), restart_after_actions=False, persistent_db_path=None):
        session = self.session
        context = tempfile.TemporaryDirectory() if persistent_db_path is None else nullcontext(None)
        with context as directory:
            path = (Path(directory) / "executor.sqlite" if persistent_db_path is None
                    else Path(persistent_db_path))
            first_date = session.events[0].now_wall.astimezone(NY).date().isoformat()
            def open_controller():
                return DryRunExecutor(path, session.account_id,
                    lambda timestamp: timestamp.astimezone(NY).strftime("%Y%m%d") in session.expirations,
                    session.max_exit_chunk, initial_trading_day=first_date)
            controller = open_controller()
            if controller.status().phase != "FLAT":
                controller.close()
                raise ReplayFormatError("Persistent replay lifecycle is active; fresh session cannot start")
            broker = SimulationFillModel()
            clock = EvidenceClock()
            blocks = []
            transitions = []
            plan = None
            max_favorable = Decimal(0)
            max_adverse = Decimal(0)
            ride_path = []
            restart_count = 0
            intent_id = "replay:" + session.session_id

            def restart():
                nonlocal controller, restart_count
                controller.close()
                controller = open_controller()
                restart_count += 1

            def transition(sequence, before):
                after = controller.status().phase
                if after != before:
                    transitions.append({"sequence": sequence, "from": before, "to": after})
                    if restart_after_actions:
                        restart()

            def snapshot(event):
                if event.broker is None:
                    raise EvidenceError("Broker evidence missing")
                return (broker.snapshot(session.account_id, event.monotonic)
                        if event.broker == "SIMULATOR" else event.broker)

            def quote_for_plan(event):
                if plan is None:
                    return None
                return next((o for o in event.options if o.contract.con_id == plan["con_id"]), None)

            def usable_quote(event):
                option = quote_for_plan(event)
                if option is None or event.spx is None:
                    raise EvidenceError("Exact option quote or SPX missing")
                if asdict(option.contract) != plan["contract"]:
                    raise EvidenceError("Exact option metadata changed during replay")
                clock.validate_pair(session.direction, event.spx, option, event.now_wall,
                                    event.monotonic,
                                    lambda t: t.astimezone(NY).strftime("%Y%m%d") in session.expirations)
                return option

            def entry_fill(event, chunk_id, qty, rejected, event_id):
                option = usable_quote(event) if qty else None
                prior = next((x for x in broker.entry_fills if x["event_id"] == event_id), None)
                if prior is not None and (prior["chunk"] != chunk_id
                        or prior["quantity"] != qty
                        or prior["price"] != str(Decimal(str(option.ask)))):
                    raise ReplayFormatError("Conflicting duplicate simulated BUY fill")
                outcome = controller.entry_event(event_id, chunk_id, qty, rejected,
                    fill_con_id=plan["con_id"] if qty else None,
                    simulated_price=option.ask if qty else None, account=event.account,
                    spx=event.spx, option=option, snapshot=snapshot(event),
                    now_wall=event.now_wall, now_monotonic=event.monotonic)
                if qty and outcome != "DUPLICATE":
                    broker.filled("ENTRY", plan["con_id"], qty, option.ask, event_id, chunk_id)
                if restart_after_actions:
                    restart()

            def exit_fill(event, chunk_id, qty, rejected, event_id):
                option = usable_quote(event) if qty else None
                prior = next((x for x in broker.exit_fills if x["event_id"] == event_id), None)
                if prior is not None and (prior["chunk"] != chunk_id
                        or prior["quantity"] != qty
                        or prior["price"] != str(Decimal(str(option.bid)))):
                    raise ReplayFormatError("Conflicting duplicate simulated SELL fill")
                outcome = controller.exit_event(event_id, chunk_id, qty, rejected,
                    fill_con_id=plan["con_id"] if qty else None,
                    simulated_price=option.bid if qty else None,
                    snapshot=snapshot(event), now_monotonic=event.monotonic)
                if qty and outcome != "DUPLICATE":
                    broker.filled("EXIT", plan["con_id"], qty, option.bid, event_id, chunk_id)
                if restart_after_actions:
                    restart()

            try:
                for event in session.events:
                    consumed_fills = set()
                    try:
                        # Every explicit broker observation must agree with the
                        # independent simulation ledger, even before controller use.
                        observed = snapshot(event)
                        validate_broker_snapshot(observed, session.account_id, event.monotonic)
                        if (observed.position_qty != broker.quantity
                                or observed.con_id != broker.con_id or observed.open_order_count):
                            raise EvidenceError("Replay broker evidence contradicts simulated fills")
                        if event.sequence == session.intent_sequence:
                            before = controller.status().phase
                            accepted = controller.press(intent_id, session.direction, observed,
                                                        event.now_wall, event.monotonic)
                            transition(event.sequence, before)
                            if accepted != "ACCEPTED":
                                raise DryRunError("Intent blocked: " + accepted)
                        if controller.status().phase == "INTENT":
                            if event.spx is None or event.account is None or not event.options:
                                raise EvidenceError("Entry evidence missing")
                            before = "INTENT"
                            plan = controller.authorize(intent_id, event.account, event.spx,
                                                        event.options, snapshot(event), event.now_wall,
                                                        event.monotonic)
                            transition(event.sequence, before)
                        if controller.status().phase == "ENTERING":
                            if session.fill_mode == "AUTO":
                                while controller.entry_chunks():
                                    chunk_id, qty = controller.entry_chunks()[0]
                                    entry_fill(event, chunk_id, qty, False,
                                               "sim-buy:" + chunk_id)
                            else:
                                for fill in (x for x in event.fills if x["side"] == "ENTRY"):
                                    consumed_fills.add(fill["event_id"])
                                    chunk_id = intent_id + ":entry:" + str(fill["chunk_index"])
                                    entry_fill(event, chunk_id, fill["quantity"],
                                               fill["rejected"], fill["event_id"])
                            if not controller.entry_chunks():
                                before = "ENTERING"
                                controller.finish_entry(snapshot(event), event.monotonic)
                                transition(event.sequence, before)
                        if controller.status().phase in {"OPEN", "EXITING"}:
                            # A missing option feed cannot create an exit fill.
                            # Valid SPX alone may still trigger protective risk.
                            if event.spx is None:
                                raise EvidenceError("SPX feed missing while position active")
                            if quote_for_plan(event) is None:
                                blocks.append({"sequence": event.sequence,
                                               "reason": "Exact option feed missing"})
                            else:
                                try:
                                    usable_quote(event)
                                except EvidenceError as exc:
                                    blocks.append({"sequence": event.sequence,
                                                   "reason": str(exc)})
                            before = controller.status().phase
                            controller.observe(intent_id + ":spx:" + str(event.sequence), event.spx,
                                               snapshot(event), event.now_wall, event.monotonic)
                            favorable = (Decimal(str(event.spx.price)) - Decimal(str(plan["spx_price"]))
                                         if session.direction == "CALL" else
                                         Decimal(str(plan["spx_price"])) - Decimal(str(event.spx.price)))
                            max_favorable = max(max_favorable, favorable)
                            max_adverse = max(max_adverse, -favorable)
                            ride_path.append(controller.status().ride)
                            transition(event.sequence, before)
                        if controller.status().phase == "EXITING":
                            if session.fill_mode == "AUTO":
                                while controller.status().owned:
                                    chunk_id, qty = controller.next_exit_chunk(snapshot(event), event.monotonic)
                                    exit_fill(event, chunk_id, qty, False,
                                              "sim-sell:" + chunk_id)
                            else:
                                for fill in (x for x in event.fills if x["side"] == "EXIT"):
                                    consumed_fills.add(fill["event_id"])
                                    prior = next((json.loads(detail) for eid, kind, detail
                                                  in controller.journal() if eid == fill["event_id"]
                                                  and kind == "EXIT_EVENT"), None)
                                    if prior is not None:
                                        exit_fill(event, prior["chunk"], fill["quantity"],
                                                  fill["rejected"], fill["event_id"])
                                        continue
                                    chunk = controller.next_exit_chunk(snapshot(event), event.monotonic)
                                    if chunk is None or not chunk[0].endswith(":exit:" + str(fill["chunk_index"])):
                                        raise ReplayFormatError("Scripted exit chunk out of order")
                                    exit_fill(event, chunk[0], fill["quantity"], fill["rejected"],
                                              fill["event_id"])
                            if controller.status().owned == 0:
                                before = "EXITING"
                                controller.confirm_flat(snapshot(event), event.monotonic)
                                transition(event.sequence, before)
                        if len(consumed_fills) != len(event.fills):
                            raise ReplayFormatError("Fill event incompatible with lifecycle phase")
                    except (EvidenceError, DryRunError, ReplayFormatError) as exc:
                        blocks.append({"sequence": event.sequence, "reason": str(exc)})
                    if event.restart or event.sequence in restart_sequences:
                        restart()
                state = controller.status()
                journal = controller.journal()
                trigger = next((json.loads(detail) for eid, kind, detail in journal
                                if eid == "exit:" + intent_id and kind == "EXIT_TRIGGERED"), None)
                high_water = [json.loads(detail)["favorable"] for eid, kind, detail in journal
                              if kind == "HIGH_WATER" and eid.startswith("peak:" + intent_id + ":")]
                strategy_events = [kind for eid, kind, _ in journal
                                   if kind in {"PROFIT_PROTECTION_ARMED", "LET_IT_RIDE", "EXIT_TRIGGERED"}
                                   and eid.endswith(intent_id)]
                selected = plan["contract"] if plan else None
                digest = hashlib.sha256(json.dumps(journal, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
                result = {
                    "format_version": FORMAT_VERSION,
                    "simulation_only": True,
                    "session_id": session.session_id,
                    "direction": session.direction,
                    "selected_contract": selected,
                    "authorized_quantity": plan["quantity"] if plan else None,
                    "simulated_entry_price": str(Decimal(str(plan["ask"]))) if plan else None,
                    "entry_chunks": plan["chunks"] if plan else [],
                    "entry_fills": broker.entry_fills,
                    "maximum_favorable_spx_points": str(max_favorable),
                    "maximum_adverse_spx_points": str(max_adverse),
                    "high_water_points": high_water,
                    "ride_path": ride_path,
                    "simulated_position_path": broker.position_path,
                    "strategy_transitions": transitions,
                    "strategy_events": strategy_events,
                    "exit_trigger": trigger,
                    "simulated_exit_price": (str(broker.sell_total / (100 * sum(
                        fill["quantity"] for fill in broker.exit_fills)))
                        if broker.exit_fills else None),
                    "exit_chunks": [json.loads(detail)["quantity"] for eid, kind, detail in journal
                                    if kind == "EXIT_CHUNK_PLANNED"
                                    and eid.startswith("chunk:" + intent_id + ":exit:")],
                    "exit_fills": broker.exit_fills,
                    "hypothetical_option_pnl_usd": (str(broker.sell_total - broker.buy_total)
                                                    if state.phase == "FLAT" and broker.entry_fills else None),
                    "final_reconciliation": ("CONFIRMED_FLAT" if state.phase == "FLAT"
                                             and broker.quantity == 0
                                             and any(row[0] == intent_id for row in controller.completed_trades())
                                             else "BLOCKED_NEW_ENTRY" if state.phase == "FLAT"
                                             else "UNRESOLVED_" + state.phase),
                    "trade_count": state.trades_today,
                    "journal_digest": digest,
                    "safety_blocks": blocks,
                    "restart_count": restart_count,
                }
                if state.phase in {"OPEN", "ENTERING", "EXITING"}:
                    result["safety_blocks"].append({"sequence": session.events[-1].sequence,
                                                     "reason": "SESSION_ENDED_" + state.phase})
                return result
            finally:
                controller.close()
