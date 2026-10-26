"""Stage 4A OFFLINE protection and cancel-before-reduce coordination.

All actions are model descriptions. Only the fake broker in tests acknowledges
them. This module has no connection, order submission, or cancellation API.
"""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib

from executor_broker_snapshot import synchronized
from executor_dry_run import DryRunError, DryRunExecutor, spx_profit_decision
from executor_paper_broker import integer, money, text_id
from executor_paper_types import PaperBrokerError
from simulation_evidence import EvidenceClock


def reduce_exit_state(ledger, data):
    action = data["action"]
    state = ledger.state.setdefault("exit_management", {
        "trade_id": None, "entry_ids": [], "entry_spx": None, "peak": None,
        "ride": False, "targets": [], "last_source": None, "exit_reason": None,
        "emergency": False, "request_ids": [], "cancellations": [],
        "flat_confirmed": not ledger.positions() and not ledger.state["intents"],
        "flat_revision": ledger.state["revision"],
    })
    if action == "ENABLE":
        return
    if action == "CONFIGURE":
        state.update({key: data[key] for key in ("trade_id", "entry_ids", "entry_spx", "targets")})
        state.update(peak=data["entry_spx"], ride=False, last_source=None, exit_reason=None,
                     flat_confirmed=False, cancellations=[])
    elif action == "EMERGENCY":
        state["emergency"] = True
        if data["request_id"] not in state["request_ids"]:
            state["request_ids"].append(data["request_id"])
        state["flat_confirmed"] = False
    elif action == "SPX":
        state.update(peak=data["peak"], ride=data["ride"], last_source=data["source"])
        if data["reason"]:
            state["exit_reason"] = state["exit_reason"] or data["reason"]
        # Research target configuration is retained, never activated by Mortificatio.
    elif action == "CANCEL":
        if data["intent_id"] not in state["cancellations"]:
            state["cancellations"].append(data["intent_id"])
    elif action == "FLAT":
        state["flat_confirmed"] = True
        state["flat_revision"] = ledger.state["revision"]
    else:
        raise PaperBrokerError("UNKNOWN_EXIT_MANAGEMENT_EVENT")


def desired_protection(ledger):
    state = ledger.state.get("exit_management")
    if not state or not state["entry_ids"]:
        raise PaperBrokerError("PROTECTION_TRADE_NOT_CONFIGURED")
    entries = [ledger.state["intents"][key] for key in state["entry_ids"]]
    refs = {item["order_ref"] for item in entries}
    quantity = ledger.positions().get(entries[0]["contract"]["con_id"], 0)
    fills = [item["record"] for item in ledger.state["executions"].values()
             if item["active"] and item["record"]["order_ref"] in refs and item["record"]["side"] == "BUY"]
    count = sum(fill["quantity"] for fill in fills)
    if count < quantity:
        raise PaperBrokerError("UNACCOUNTED_ENTRY_PREMIUM")
    average = (sum((money(fill["price"]) * fill["quantity"] for fill in fills), Decimal(0)) / count
               if count else None)
    try:
        stop = DryRunExecutor._stop_price(average) if quantity else None
    except DryRunError as exc:
        raise PaperBrokerError("INVALID_FILLED_PREMIUM_STOP") from exc
    return {"quantity": quantity, "average_entry_premium": str(average) if average is not None else None,
            "stop_price": str(stop) if stop is not None else None,
            "contract": entries[0]["contract"]}


class ProtectiveExitManager:
    def __init__(self, ledger, emergency=None):
        self.ledger, self.lock, self.emergency = ledger, ledger.lock, emergency
        ledger.emergency = emergency
        if "exit_management" not in ledger.state:
            ledger._append("exit-management-enable", "EXIT_MANAGEMENT", {"action": "ENABLE"})

    @property
    def state(self):
        return self.ledger.state["exit_management"]

    def _event(self, key, data):
        return self.ledger._append("xm:" + key, "EXIT_MANAGEMENT", data)

    @synchronized
    def configure_trade(self, trade_id, entry_ids, entry_spx, targets=()):
        if (not text_id(trade_id) or not isinstance(entry_ids, (tuple, list)) or not entry_ids
                or len(set(entry_ids)) != len(entry_ids) or self.state["emergency"]
                or self.state["trade_id"] is not None and not self.state["flat_confirmed"]):
            raise PaperBrokerError("EXIT_TRADE_CONFIGURATION_BLOCKED")
        entries = [self.ledger.state["intents"].get(key) for key in entry_ids]
        if (any(item is None or item["side"] != "BUY" for item in entries)
                or len({item["contract"]["con_id"] for item in entries}) != 1):
            raise PaperBrokerError("EXACT_ENTRY_INTENT_GROUP_REQUIRED")
        total = sum(item["quantity"] for item in entries)
        if total > 25:
            raise PaperBrokerError("ENTRY_GROUP_EXCEEDS_CAP")
        policies, ids, target_total = [], set(), 0
        for target in targets:
            if (not isinstance(target, dict) or set(target) != {"id", "spx_points", "quantity"}
                    or not text_id(target["id"]) or target["id"] in ids
                    or not integer(target["quantity"], 1, 25)):
                raise PaperBrokerError("EXPLICIT_PARTIAL_EXIT_POLICY_REQUIRED")
            level = money(target["spx_points"], True)
            ids.add(target["id"])
            target_total += target["quantity"]
            policies.append({"id": target["id"], "spx_points": str(level), "quantity": target["quantity"], "due": False})
        if target_total > total:
            raise PaperBrokerError("PARTIAL_EXIT_QUANTITIES_EXCEED_ENTRY_PLAN")
        self._event("configure:"+trade_id, {"action": "CONFIGURE", "trade_id": trade_id,
                    "entry_ids": list(entry_ids), "entry_spx": str(money(entry_spx, True)), "targets": policies})

    @synchronized
    def exit_all(self, request_id):
        if not text_id(request_id):
            raise PaperBrokerError("STABLE_EMERGENCY_REQUEST_REQUIRED")
        if self.emergency is not None:
            self.emergency.request(request_id)
        return self._event("emergency:"+request_id, {"action": "EMERGENCY", "request_id": request_id})

    @synchronized
    def observe_spx(self, event_id, spx):
        if not self.state["trade_id"]:
            raise PaperBrokerError("NO_CONFIGURED_TRADE")
        # Exact duplicate events are handled before freshness checks on replay.
        key = "xm:spx:"+event_id
        prior = self.ledger.db.execute("SELECT payload FROM events WHERE event_id=?", (key,)).fetchone()
        clock = EvidenceClock()
        if self.state["last_source"]:
            clock.last_source["SPX"] = datetime.fromisoformat(self.state["last_source"])
        if not prior:
            if not self.ledger.positions():
                raise PaperBrokerError("NO_OWNED_POSITION_FOR_PROFIT_MONITORING")
            clock.validate_spx(spx, self.ledger.wall_clock(), self.ledger.monotonic_clock())
        favorable, _, peak, ride, _, reason = spx_profit_decision(
            "CALL" if self.ledger.state["intents"][self.state["entry_ids"][0]]["contract"]["right"] == "C" else "PUT",
            Decimal(self.state["entry_spx"]), Decimal(self.state["peak"]), self.state["ride"], Decimal(str(spx.price)))
        if prior:
            import json
            data = json.loads(prior[0])
            if data["price"] != str(spx.price) or data["source"] != spx.source_time.isoformat() or data["status"] != spx.status.value or data["receipt"] != spx.received_monotonic:
                raise PaperBrokerError("CHANGED_SPX_EXIT_EVENT")
            return "DUPLICATE"
        return self._event("spx:"+event_id, {"action": "SPX", "peak": str(peak), "ride": ride,
                           "reason": reason, "favorable": str(favorable), "price": str(spx.price),
                           "source": spx.source_time.isoformat(), "status": spx.status.value,
                           "receipt": spx.received_monotonic})

    def _prefix(self, role):
        token = hashlib.sha256((self.state["trade_id"]+"|"+role).encode()).hexdigest()[:24]
        return "px:"+token+":"

    def _intents(self, role):
        prefix = self._prefix(role)
        return [item for item in self.ledger.state["intents"].values() if item["intent_id"].startswith(prefix)]

    def _fresh(self):
        try:
            self.ledger._planning_gate(self.ledger.state["generation"])
            return True
        except PaperBrokerError:
            return False

    def _partial(self):
        for target in self.state["targets"]:
            filled = sum(self.ledger.filled(item) for item in self._intents("TARGET:"+target["id"]))
            if target["due"] and filled < target["quantity"]:
                return target, target["quantity"]-filled
        return None, 0

    @synchronized
    def status(self):
        base = {"executable": False, "submission_enabled": False, "emergency": self.state["emergency"],
                "phase": "RECOVERY REQUIRED", "coverage": "UNVERIFIED", "configured_targets": deepcopy(self.state["targets"]),
                "automatic_profit_scale_outs": False}
        if self.emergency is not None and self.emergency.blocked() and not self.state["emergency"]:
            self.exit_all(self.emergency.status()["request_id"])
            base["emergency"] = True
        if not self.state["trade_id"]:
            return base
        try:
            desired = desired_protection(self.ledger)
        except PaperBrokerError:
            if not self.state["emergency"]:
                self.exit_all("invalid-premium:"+self.state["trade_id"])
            contract = self.ledger.state["intents"][self.state["entry_ids"][0]]["contract"]
            base.update(emergency=True, coverage="INVALID_PREMIUM_STOP", phase="EXIT ALL PENDING",
                        quantity=self.ledger.positions().get(contract["con_id"], 0), contract=dict(contract),
                        average_entry_premium=None, stop_price=None)
            return base
        base.update(desired)
        fresh = self._fresh()
        stops = [item for item in self._intents("STOP") if not item["terminal_proven"]]
        rejected = [item for item in stops if item["status"] == "Inactive"]
        if rejected and not self.state["emergency"]:
            self.exit_all("rejected-stop:"+rejected[0]["intent_id"])
        covered = (fresh and len(stops) == 1 and stops[0]["order_id"] is not None
                   and stops[0]["status"] in {"Submitted", "PreSubmitted"} and not stops[0]["uncertain"]
                   and stops[0]["quantity"]-self.ledger.filled(stops[0]) == desired["quantity"]
                   and stops[0]["stop_price"] == desired["stop_price"])
        flat = (fresh and self.state["flat_confirmed"] and self.state["flat_revision"] == self.ledger.state["revision"]
                and not self.ledger.positions() and all(item["terminal_proven"] for item in self.ledger.state["intents"].values()))
        base.update(emergency=self.state["emergency"],
                    phase="FLAT CONFIRMED" if flat else "EXIT ALL PENDING" if self.state["emergency"] else
                          "EXIT PENDING" if self.state["exit_reason"] else "OPEN" if fresh else "RECOVERY REQUIRED",
                    coverage="BROKER_HELD_MODEL_ACKNOWLEDGED" if covered and desired["quantity"] else
                             "NO_POSITION" if not desired["quantity"] else "UNPROTECTED_OR_RESIZE_PENDING")
        return base

    @synchronized
    def advance(self):
        status = self.status()
        # A never-bound local stop cannot exist at the broker. Safely abandon
        # stale local geometry even when its excessive reservation blocks the
        # next reconciliation; never apply this shortcut to a bound order.
        if self.state["trade_id"] and "quantity" in status:
            stale = [item for item in self._intents("STOP") if item["order_id"] is None
                     and not item["terminal_proven"] and (item["quantity"] != status["quantity"]
                     or item["stop_price"] != status["stop_price"])]
            if stale:
                try:
                    for item in stale:
                        self.ledger.abandon_unbound_intent(self.ledger.state["generation"], item["intent_id"])
                except PaperBrokerError:
                    return [{"type": "RECONCILE_REQUIRED", "executable": False}]
                return [{"type": "LOCAL_ABANDONED", "intent_id": item["intent_id"], "executable": False} for item in stale]
        if not self._fresh() or not self.state["trade_id"]:
            return [{"type": "RECONCILE_REQUIRED", "executable": False}]
        desired = status if status["coverage"] == "INVALID_PREMIUM_STOP" else desired_protection(self.ledger)
        quantity = desired["quantity"]
        # Keep partial-exit research infrastructure, but never execute targets
        # in the active direction-only strategy (including recovered targets).
        target, target_qty = None, 0
        full = self.state["emergency"] or self.state["exit_reason"]
        role = "FULL" if full else "TARGET:"+target["id"] if target else "STOP"
        current = self._intents(role)
        keep = [item for item in current if not item["terminal_proven"]]
        if role == "STOP":
            keep = [item for item in keep if item["quantity"]-self.ledger.filled(item) == quantity
                    and item["stop_price"] == desired["stop_price"]]
        keep_ids = {item["intent_id"] for item in keep}
        acquisitions = [item for item in self.ledger.state["intents"].values()
                        if item["side"] == "BUY" and not item["terminal_proven"]]
        blockers = [item for item in self.ledger.state["intents"].values()
                    if not item["terminal_proven"] and item["intent_id"] not in keep_ids
                    and (role != "STOP" or item["side"] == "SELL")]
        if full and acquisitions:
            # Cancel acquisition first while retaining any acknowledged hard stop.
            # Late fills revise owned quantity; reconciliation remains mandatory.
            blockers = acquisitions
        if blockers:
            actions = []
            for item in blockers:
                if item["order_id"] is None:
                    self.ledger.abandon_unbound_intent(self.ledger.state["generation"], item["intent_id"])
                    actions.append({"type": "LOCAL_ABANDONED", "intent_id": item["intent_id"], "executable": False})
                else:
                    self._event("cancel:"+item["intent_id"], {"action": "CANCEL", "intent_id": item["intent_id"]})
                    actions.append({"type": "CANCEL_REQUIRED", "intent_id": item["intent_id"], "order_id": item["order_id"], "executable": False})
            return actions
        if not quantity:
            if self.ledger.positions() or any(not item["terminal_proven"] for item in self.ledger.state["intents"].values()):
                return [{"type": "RECONCILE_REQUIRED", "executable": False}]
            self._event("flat:{}:{}".format(self.state["trade_id"], self.ledger.state["revision"]), {"action": "FLAT"})
            if self.emergency is not None and self.state["emergency"]:
                self.emergency.confirm_flat("paper-cycle:{}:{}".format(self.ledger.state["generation"], self.ledger.state["last_cycle"]))
            return [{"type": "FLAT_CONFIRMED", "executable": False}]
        if keep:
            return [{"type": "BIND_REQUIRED" if item["order_id"] is None else "WAIT_BROKER_EVIDENCE",
                     "intent_id": item["intent_id"], "executable": False} for item in keep]
        intent_id = self._prefix(role)+str(len(current))
        purpose = ("PROTECTIVE_STOP" if role == "STOP" else "PROFIT_PARTIAL" if target and not full else
                   "EMERGENCY_EXIT" if self.state["emergency"] else "STRATEGY_EXIT")
        from simulation_evidence import VerifiedOptionContract
        self.ledger.prepare_intent(self.ledger.state["generation"], intent_id, intent_id,
                                   VerifiedOptionContract(**desired["contract"]), "SELL",
                                   min(quantity, target_qty) if target and not full else quantity, lambda _: True,
                                   purpose=purpose, stop_price=desired["stop_price"] if role == "STOP" else None)
        return [{"type": "BIND_REQUIRED", "intent_id": intent_id, "executable": False}]
