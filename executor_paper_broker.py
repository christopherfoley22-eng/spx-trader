"""Stage 3A: isolated durable paper ledger; submission is permanently disabled.

Only normalized offline fixtures can verify sessions in this stage. There is
no socket, EClient, outbound broker capability, or connection to the Executor.
Order statuses are observations, never execution or cancellation guarantees.
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from threading import RLock
import time

from executor_broker_snapshot import synchronized
from executor_paper_types import (
    ApprovedPaperAccount, BrokerOrderRecord, CommissionRecord, ExecutionRecord,
    PaperBrokerError, PaperSessionEvidence, TERMINAL, WORKING,
)
from executor_redaction import safe_error_text
from safe_sizing import MAX_CONTRACTS
from mortificatio_store import private_database
from simulation_evidence import MAX_AGE_SECONDS, VerifiedOptionContract, validate_contract


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PaperBrokerError("UTC_EVIDENCE_REQUIRED")
    return value.astimezone(timezone.utc)


def integer(value, minimum=0, maximum=2**63-1):
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def money(value, positive=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) >= Decimal("1e100") or positive and amount <= 0:
            raise ValueError
        return amount
    except (InvalidOperation, TypeError, ValueError):
        raise PaperBrokerError("INVALID_FINANCIAL_VALUE")


def text_id(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def execution_family(exec_id):
    prefix, separator, revision = exec_id.rpartition(".")
    return (prefix, int(revision)) if separator and prefix and revision.isdigit() else (exec_id, None)


class PaperBrokerLedger:
    """One durable writer; all intents and economic callbacks are journaled first."""

    def __init__(self, path, approval, wall_clock=None, monotonic_clock=None):
        if (not isinstance(approval, ApprovedPaperAccount)
                or not isinstance(approval.account, str)
                or not re.fullmatch(r"DU[0-9]{5,}", approval.account)
                or not text_id(approval.verification_ref)
                or approval.host != "127.0.0.1" or type(approval.port) is not int
                or approval.port not in (7497, 4002)):
            raise PaperBrokerError("EXPLICIT_APPROVED_PAPER_ACCOUNT_REQUIRED")
        utc(approval.approved_until)
        self.approval = approval
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.lock = RLock()
        self.session = None
        self.last_receipt = None
        self.reconciled_at = None
        self.path = private_database(path)
        self.lock_file = private_database(self.path.with_name(self.path.name + ".lock")).open("a+b")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise PaperBrokerError("PAPER_LEDGER_WRITER_ALREADY_ACTIVE")
        try:
            self.db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
            self.path.chmod(0o600)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    version INTEGER NOT NULL, approval_hash TEXT NOT NULL,
                    sequence INTEGER NOT NULL, head TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
            """)
            config = asdict(approval)
            config["approved_until"] = utc(approval.approved_until).isoformat()
            binding = hashlib.sha256(canonical(config).encode()).hexdigest()
            previous = self.db.execute("SELECT version,approval_hash FROM binding").fetchone()
            if previous is None:
                if self.db.execute("SELECT count(*) FROM events").fetchone()[0]:
                    raise PaperBrokerError("LEDGER_BINDING_MISSING")
                self.db.execute("INSERT INTO binding VALUES (1,1,?,0,'')", (binding,))
            elif previous != (1, binding):
                raise PaperBrokerError("PERSISTED_PAPER_APPROVAL_MISMATCH")
            self._load()
            if self.sequence:
                self._append("restart:" + str(self.state["restarts"]+1), "RESTART", {})
        except Exception:
            self.close()
            raise

    @synchronized
    def close(self):
        if hasattr(self, "db"):
            self.db.close()
        if not self.lock_file.closed:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()

    @staticmethod
    def _empty():
        return {"generation": 0, "nonces": [], "intents": {}, "executions": {},
                "commissions": {}, "faults": [], "revision": 0, "restarts": 0,
                "reconciled_revision": -1, "last_cycle": 0}

    def _load(self):
        if self.db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise PaperBrokerError("PAPER_LEDGER_CORRUPT")
        self.state, self.head, self.sequence = self._empty(), "", 0
        try:
            for seq, event_id, kind, raw, digest in self.db.execute("SELECT * FROM events ORDER BY sequence"):
                payload = json.loads(raw)
                expected = hashlib.sha256(canonical([self.head, seq, event_id, kind, raw]).encode()).hexdigest()
                if seq != self.sequence+1 or digest != expected or canonical(payload) != raw:
                    raise PaperBrokerError("PAPER_JOURNAL_INTEGRITY_FAILURE")
                self._reduce(kind, payload)
                self.head, self.sequence = digest, seq
            if self.db.execute("SELECT sequence,head FROM binding").fetchone() != (self.sequence, self.head):
                raise PaperBrokerError("PAPER_JOURNAL_HEAD_MISMATCH")
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
            raise PaperBrokerError("PAPER_JOURNAL_UNVERIFIABLE") from exc

    def _rollback_snapshot(self,kind,payload):
        return deepcopy(self.state)

    def _append(self, event_id, kind, payload):
        if not text_id(event_id):
            raise PaperBrokerError("STABLE_EVENT_ID_REQUIRED")
        raw = canonical(payload)
        old = self.db.execute("SELECT kind,payload FROM events WHERE event_id=?", (event_id,)).fetchone()
        if old:
            if old != (kind, raw):
                raise PaperBrokerError("CONFLICTING_DUPLICATE_EVENT")
            return "DUPLICATE"
        previous = self._rollback_snapshot(kind,payload)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._reduce(kind, payload)
            seq = self.sequence + 1
            digest = hashlib.sha256(canonical([self.head, seq, event_id, kind, raw]).encode()).hexdigest()
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (seq, event_id, kind, raw, digest))
            self.db.execute("UPDATE binding SET sequence=?,head=?", (seq, digest))
            self.db.execute("COMMIT")
            self.sequence, self.head = seq, digest
            return "RECORDED"
        except Exception:
            self.db.execute("ROLLBACK")
            self.state = previous
            raise

    def _reduce(self, kind, data):
        state = self.state
        intents = state["intents"]
        if kind == "SESSION":
            if data["generation"] != state["generation"]+1 or data["nonce"] in state["nonces"]:
                raise PaperBrokerError("SESSION_GENERATION_OR_NONCE_REUSED")
            state["generation"] = data["generation"]
            state["nonces"].append(data["nonce"])
            state["last_cycle"] = 0
        elif kind in {"RESTART", "DISCONNECT"}:
            if kind == "RESTART":
                state["restarts"] += 1
            for intent in intents.values():
                if intent["order_id"] is not None:
                    intent["uncertain"], intent["terminal_proven"] = True, False
        elif kind == "FAULT":
            state["faults"].append(data)
        elif kind == "EXIT_MANAGEMENT":
            from executor_protective_exits import reduce_exit_state
            reduce_exit_state(self, data)
            return  # Control metadata does not refresh/invalidate broker evidence.
        elif kind == "LOCAL_ABANDON":
            intent = intents[data["intent_id"]]
            if intent["order_id"] is not None or self.filled(intent):
                raise PaperBrokerError("BOUND_ORDER_CANNOT_BE_ABANDONED")
            intent["terminal_proven"], intent["status"] = True, "LOCAL_ABANDONED"
        elif kind == "INTENT":
            if data["intent_id"] in intents or any(i["order_ref"] == data["order_ref"] for i in intents.values()):
                raise PaperBrokerError("INTENT_OR_ORDER_REF_REUSED")
            intents[data["intent_id"]] = dict(data, order_id=None, perm_id=None, client_id=None,
                                             status="PREPARED", reported_filled=0,
                                             uncertain=False, terminal_proven=False)
        elif kind == "BIND":
            intent = intents[data["intent_id"]]
            if intent["order_id"] is not None:
                raise PaperBrokerError("INTENT_ALREADY_HAS_ORDER_ID_NO_RETRY")
            for item in intents.values():
                if (item["client_id"], item["order_id"]) == (data["client_id"], data["order_id"]):
                    raise PaperBrokerError("ORDER_ID_REUSED")
            intent.update(order_id=data["order_id"], client_id=data["client_id"], uncertain=True,
                          status="RESERVED_UNCONFIRMED")
        elif kind == "ORDER":
            intent = self._identity(data)
            old_status = intent["status"]
            if data["perm_id"] == 0:
                intent["uncertain"] = True
            elif old_status not in TERMINAL or data["status"] in TERMINAL:
                intent["status"] = data["status"]
                intent["reported_filled"] = data["filled"]
            intent["terminal_proven"] = False
        elif kind == "EXECUTION":
            intent = self._identity(data)
            executions = state["executions"]
            if data["exec_id"] in executions:
                if executions[data["exec_id"]]["record"] != data:
                    raise PaperBrokerError("CONFLICTING_EXECUTION_ID")
            else:
                family, revision = execution_family(data["exec_id"])
                same_family = [item for item in executions.values()
                               if execution_family(item["record"]["exec_id"])[0] == family]
                prior = executions.get(data["correction_of"])
                if data["correction_of"]:
                    if (prior is None or not prior["active"] or revision is None
                            or execution_family(prior["record"]["exec_id"])[0] != family
                            or execution_family(prior["record"]["exec_id"])[1] is None
                            or revision <= execution_family(prior["record"]["exec_id"])[1]
                            or any(prior["record"][key] != data[key] for key in
                                   ("account", "order_ref", "order_id", "perm_id", "client_id", "contract", "side"))):
                        raise PaperBrokerError("EXECUTION_CORRECTION_CHAIN_UNVERIFIABLE")
                    prior["active"] = False
                elif same_family or data["bust"]:
                    raise PaperBrokerError("EXPLICIT_CORRECTION_REQUIRED")
                executions[data["exec_id"]] = {"record": data, "active": True}
                intent["terminal_proven"] = False
                if self.filled(intent) > intent["quantity"]:
                    raise PaperBrokerError("EXECUTION_EXCEEDS_INTENT")
                if any(qty < 0 or qty > MAX_CONTRACTS for qty in self.positions().values()):
                    raise PaperBrokerError("SHORT_OR_EXCESS_POSITION_OBSERVED")
                if state.get("exit_management"):
                    state["exit_management"]["flat_confirmed"] = False
                if intent.get("purpose") == "PROTECTIVE_STOP" and data["quantity"]:
                    management = state.get("exit_management")
                    if management:
                        management["exit_reason"] = "INITIAL_STOP"
        elif kind == "COMMISSION":
            old = state["commissions"].get(data["exec_id"])
            if old == data:
                return
            if old:
                unchanged = (old["amount"], old["currency"]) == (data["amount"], data["currency"])
                if (not unchanged and data["correction_of"] != old["report_id"]
                        or unchanged and data["correction_of"] not in (None, old["report_id"])):
                    raise PaperBrokerError("COMMISSION_CORRECTION_REQUIRED")
            elif data["correction_of"]:
                raise PaperBrokerError("UNKNOWN_COMMISSION_CORRECTION")
            state["commissions"][data["exec_id"]] = data
        elif kind == "RECONCILED":
            state["last_cycle"] = data["cycle_id"]
            for intent_id, terminal in data["orders"].items():
                intents[intent_id]["uncertain"] = False
                intents[intent_id]["terminal_proven"] = terminal
            state["reconciled_revision"] = state["revision"]
            return
        else:
            raise PaperBrokerError("UNKNOWN_PAPER_JOURNAL_EVENT")
        state["revision"] += 1

    def _identity(self, data):
        matches = [intent for intent in self.state["intents"].values() if intent["order_ref"] == data["order_ref"]]
        if len(matches) != 1 or data["account"] != self.approval.account:
            raise PaperBrokerError("UNKNOWN_ORDER_OR_ACCOUNT")
        intent = matches[0]
        for key in ("contract", "side", "order_id", "client_id"):
            if intent[key] != data[key]:
                raise PaperBrokerError("ORDER_IDENTITY_MISMATCH")
        if data["perm_id"] and intent["perm_id"] is not None and intent["perm_id"] != data["perm_id"]:
            raise PaperBrokerError("PERM_ID_CHANGED")
        if data["perm_id"] and any(item is not intent and item["perm_id"] == data["perm_id"]
               for item in self.state["intents"].values()):
            raise PaperBrokerError("PERM_ID_REUSED")
        if data["perm_id"]:
            intent["perm_id"] = data["perm_id"]
        if "remaining" in data and (data["quantity"] != intent["quantity"]
                                     or data["filled"] + data["remaining"] != intent["quantity"]):
            raise PaperBrokerError("BROKER_ORDER_QUANTITY_MISMATCH")
        if "remaining" in data and intent.get("purpose") == "PROTECTIVE_STOP":
            if data.get("order_type") != "STP" or money(data.get("stop_price"), True) != money(intent["stop_price"], True):
                raise PaperBrokerError("PROTECTIVE_STOP_ACK_MISMATCH")
        return intent

    def filled(self, intent):
        return sum(item["record"]["quantity"] for item in self.state["executions"].values()
                   if item["active"] and item["record"]["order_ref"] == intent["order_ref"])

    def positions(self):
        result = {}
        for item in self.state["executions"].values():
            if item["active"]:
                record = item["record"]
                con_id = record["contract"]["con_id"]
                result[con_id] = result.get(con_id, 0) + record["quantity"] * (1 if record["side"] == "BUY" else -1)
        return {con_id: qty for con_id, qty in result.items() if qty}

    def reservations(self, side, con_id):
        return sum(max(0, item["quantity"] - self.filled(item)) for item in self.state["intents"].values()
                   if item["side"] == side and item["contract"]["con_id"] == con_id and not item["terminal_proven"])

    def _fault(self, reason, payload=None):
        self.reconciled_at = None
        self._append("fault:" + str(self.sequence+1), "FAULT",
                     {"reason": safe_error_text(reason), "rejected_evidence": payload})
        raise PaperBrokerError(reason)

    def _session(self, generation):
        if self.session is None or generation != self.state["generation"]:
            raise PaperBrokerError("VERIFIED_PAPER_SESSION_REQUIRED")
        now = utc(self.wall_clock())
        mono = self.monotonic_clock()
        if (not isinstance(mono, (int, float)) or isinstance(mono, bool)
                or not math.isfinite(mono) or mono < 0
                or self.last_receipt is not None and mono < self.last_receipt
                or now < utc(self.session.verified_at)):
            self.session = None
            self._fault("SESSION_CLOCK_ROLLBACK_OR_INVALID")
        if now >= min(utc(self.session.expires_at), utc(self.approval.approved_until)):
            self.disconnect(generation)
            raise PaperBrokerError("PAPER_SESSION_EXPIRED")
        self.last_receipt = mono
        return mono

    @synchronized
    def verify_session(self, evidence):
        if self.session is not None:
            raise PaperBrokerError("END_PREVIOUS_PAPER_SESSION_FIRST")
        if not isinstance(evidence, PaperSessionEvidence):
            raise PaperBrokerError("PAPER_SESSION_EVIDENCE_REQUIRED")
        now = utc(self.wall_clock())
        if (evidence.origin != "OFFLINE_FIXTURE" or evidence.mode != "PAPER"
                or evidence.read_only is not True or evidence.account != self.approval.account
                or evidence.managed_accounts != (self.approval.account,)
                or evidence.verification_ref != self.approval.verification_ref
                or (evidence.host, evidence.port) != (self.approval.host, self.approval.port)
                or not integer(evidence.client_id, 1) or not text_id(evidence.nonce)
                or not 0 <= (now - utc(evidence.verified_at)).total_seconds() <= MAX_AGE_SECONDS
                or not now < utc(evidence.expires_at) <= utc(self.approval.approved_until)):
            raise PaperBrokerError("LIVE_OR_UNVERIFIED_SESSION_REJECTED")
        generation = self.state["generation"]+1
        self._append("session:" + str(generation), "SESSION", {"generation": generation, "nonce": evidence.nonce})
        self.session = evidence
        self.last_receipt = None
        self.reconciled_at = None
        self._session(generation)
        return generation

    @synchronized
    def disconnect(self, generation):
        if self.session is None or generation != self.state["generation"]:
            raise PaperBrokerError("OLD_SESSION_DISCONNECT")
        self._append("disconnect:" + str(generation), "DISCONNECT", {})
        self.session = None
        self.reconciled_at = None

    def _planning_gate(self, generation):
        now = self._session(generation)
        if (self.state["faults"] or self.reconciled_at is None
                or not 0 <= now - self.reconciled_at <= MAX_AGE_SECONDS
                or self.state["revision"] != self.state["reconciled_revision"]):
            raise PaperBrokerError("FRESH_COMPLETE_RECONCILIATION_REQUIRED")

    @synchronized
    def prepare_intent(self, generation, intent_id, order_ref, contract, side, quantity,
                       session_has_expiration, purpose="STANDARD", stop_price=None):
        self._session(generation)
        if (not text_id(intent_id) or not text_id(order_ref) or side not in {"BUY", "SELL"}
                or not isinstance(contract, VerifiedOptionContract) or not integer(quantity, 1, MAX_CONTRACTS)):
            raise PaperBrokerError("INVALID_ORDER_INTENTION")
        validate_contract(contract, "CALL" if contract.right == "C" else "PUT",
                          self.wall_clock(), session_has_expiration)
        payload = {"intent_id": intent_id, "order_ref": order_ref, "contract": asdict(contract),
                   "side": side, "quantity": quantity, "created_at": utc(self.wall_clock()).isoformat()}
        if purpose not in {"STANDARD", "PROTECTIVE_STOP", "PROFIT_PARTIAL", "STRATEGY_EXIT", "EMERGENCY_EXIT"}:
            raise PaperBrokerError("UNKNOWN_OFFLINE_ORDER_PURPOSE")
        if purpose != "STANDARD":
            if side != "SELL" or not self.state.get("exit_management"):
                raise PaperBrokerError("EXIT_MANAGER_REQUIRED")
            payload["purpose"] = purpose
            payload["stop_price"] = str(money(stop_price, True)) if purpose == "PROTECTIVE_STOP" else None
        elif stop_price is not None:
            raise PaperBrokerError("UNEXPECTED_STOP_PRICE")
        old = self.state["intents"].get(intent_id)
        if old:
            if (any(old[key] != payload[key] for key in ("order_ref", "contract", "side", "quantity"))
                    or old.get("purpose", "STANDARD") != purpose or old.get("stop_price") != payload.get("stop_price")):
                self._fault("CONFLICTING_INTENT_REPLAY")
            return "DUPLICATE"
        self._planning_gate(generation)
        management = self.state.get("exit_management")
        if side == "BUY" and (getattr(self, "emergency", None) is not None and self.emergency.blocked()
                               or management and (management["emergency"] or not management["flat_confirmed"])):
            raise PaperBrokerError("EXIT_MANAGEMENT_BLOCKS_NEW_ENTRY")
        positions = self.positions()
        con_id = contract.con_id
        if any(item["uncertain"] for item in self.state["intents"].values()):
            raise PaperBrokerError("UNCERTAIN_ORDER_NO_RETRY")
        if (any(cid != con_id for cid in positions)
                or any(item["contract"]["con_id"] != con_id and not item["terminal_proven"]
                       for item in self.state["intents"].values())):
            raise PaperBrokerError("DIFFERENT_ACTIVE_CONTRACT")
        if side == "BUY":
            if (self.reservations("SELL", con_id) or positions.get(con_id, 0) +
                    self.reservations("BUY", con_id) + quantity > MAX_CONTRACTS):
                raise PaperBrokerError("ENTRY_CAP_OR_EXIT_UNRESOLVED")
        elif ((purpose != "PROTECTIVE_STOP" and self.reservations("BUY", con_id)) or quantity + self.reservations("SELL", con_id)
              > positions.get(con_id, 0)):
            raise PaperBrokerError("EXIT_WOULD_EXCEED_OWNED_POSITION")
        if purpose == "PROTECTIVE_STOP":
            from executor_protective_exits import desired_protection
            desired = desired_protection(self)
            if quantity != desired["quantity"] or money(stop_price, True) != money(desired["stop_price"], True):
                raise PaperBrokerError("STOP_PLAN_MUST_MATCH_FILLED_PREMIUM_AND_QUANTITY")
        return self._append("intent:" + intent_id, "INTENT", payload)

    @synchronized
    def abandon_unbound_intent(self, generation, intent_id):
        self._session(generation)
        return self._append("abandon:" + intent_id, "LOCAL_ABANDON", {"intent_id": intent_id})

    @synchronized
    def reserve_order_identity(self, generation, intent_id, order_id):
        self._session(generation)
        if intent_id not in self.state["intents"] or not integer(order_id, 0):
            raise PaperBrokerError("UNKNOWN_INTENT_OR_INVALID_ORDER_ID")
        intent = self.state["intents"][intent_id]
        if intent["status"] == "LOCAL_ABANDONED":
            raise PaperBrokerError("ABANDONED_INTENT_CANNOT_BE_BOUND")
        if intent["order_id"] is not None:
            if (intent["order_id"], intent["client_id"]) == (order_id, self.session.client_id):
                return "DUPLICATE"
            raise PaperBrokerError("UNCERTAIN_ORDER_NO_RETRY")
        management = self.state.get("exit_management")
        if intent["side"] == "BUY" and (getattr(self, "emergency", None) is not None and self.emergency.blocked()
                or management and (management["emergency"] or management["exit_reason"]
                                   or any(target["due"] for target in management["targets"]))):
            raise PaperBrokerError("EXIT_MANAGEMENT_BLOCKS_ENTRY_BINDING")
        if any(item["uncertain"] for item in self.state["intents"].values()):
            raise PaperBrokerError("UNCERTAIN_ORDER_NO_RETRY")
        self._planning_gate(generation)
        con_id = intent["contract"]["con_id"]
        if intent.get("purpose") == "PROTECTIVE_STOP":
            from executor_protective_exits import desired_protection
            desired = desired_protection(self)
            if intent["quantity"] != desired["quantity"] or money(intent["stop_price"], True) != money(desired["stop_price"], True):
                raise PaperBrokerError("STALE_PROTECTIVE_STOP_PLAN")
        if intent["side"] == "SELL" and self.reservations("SELL", con_id) > self.positions().get(con_id, 0):
            raise PaperBrokerError("EXIT_WOULD_EXCEED_OWNED_POSITION")
        return self._append("bind:" + intent_id, "BIND",
                            {"intent_id": intent_id, "order_id": order_id, "client_id": self.session.client_id})

    def _record(self, generation, event_id, kind, payload):
        self._session(generation)
        try:
            return self._append(event_id, kind, payload)
        except PaperBrokerError as exc:
            self._fault(str(exc), payload)

    @synchronized
    def observe_order(self, generation, event_id, record):
        self._session(generation)
        if (not isinstance(record, BrokerOrderRecord) or record.status not in WORKING | TERMINAL | {"Inactive"}
                or not integer(record.order_id) or not integer(record.client_id, 1)
                or not integer(record.perm_id, 0) or not integer(record.quantity, 1, MAX_CONTRACTS)
                or not integer(record.filled, 0, MAX_CONTRACTS) or not integer(record.remaining, 0, MAX_CONTRACTS)
                or record.status == "Filled" and (record.remaining or record.filled != record.quantity)):
            self._fault("INVALID_ORDER_OBSERVATION")
        payload = asdict(record)
        # Preserve canonical Stage 3A callback payloads when the new optional
        # stop fields are unused, including duplicate IDs after an upgrade.
        if record.order_type == "UNSPECIFIED" and record.stop_price is None:
            payload.pop("order_type")
            payload.pop("stop_price")
        return self._record(generation, event_id, "ORDER", payload)

    @synchronized
    def observe_execution(self, generation, event_id, record):
        self._session(generation)
        if (not isinstance(record, ExecutionRecord) or not text_id(record.exec_id)
                or not integer(record.order_id) or not integer(record.perm_id, 1)
                or not integer(record.client_id, 1) or type(record.bust) is not bool
                or not integer(record.quantity, 0 if record.bust else 1, MAX_CONTRACTS)
                or record.bust and (record.quantity != 0 or not record.correction_of)):
            self._fault("INVALID_EXECUTION_OBSERVATION")
        payload = asdict(record)
        try:
            payload["price"] = str(money(record.price, positive=not record.bust))
        except PaperBrokerError:
            self._fault("INVALID_EXECUTION_PRICE")
        return self._record(generation, event_id, "EXECUTION", payload)

    @synchronized
    def observe_commission(self, generation, event_id, record):
        self._session(generation)
        if (not isinstance(record, CommissionRecord) or record.currency != "USD"
                or not text_id(record.exec_id) or not text_id(record.report_id)):
            self._fault("INVALID_COMMISSION_OBSERVATION")
        payload = asdict(record)
        try:
            payload["amount"] = str(money(record.amount))
        except PaperBrokerError:
            self._fault("INVALID_COMMISSION_AMOUNT")
        return self._record(generation, event_id, "COMMISSION", payload)

    @synchronized
    def reconcile(self, snapshot):
        from executor_paper_reconciliation import reconcile_paper_snapshot
        return reconcile_paper_snapshot(self, snapshot)

    @synchronized
    def status(self):
        verified = False
        if self.session is not None:
            try:
                self._session(self.state["generation"])
                verified = True
            except PaperBrokerError:
                pass
        reasons = []
        if not verified:
            reasons.append("VERIFIED_PAPER_SESSION_REQUIRED")
        if self.state["faults"]:
            reasons.append("QUARANTINED_BROKER_EVIDENCE_REQUIRES_REVIEW")
        try:
            self._planning_gate(self.state["generation"])
        except PaperBrokerError:
            reasons.append("FRESH_COMPLETE_RECONCILIATION_REQUIRED")
        if any(item["uncertain"] for item in self.state["intents"].values()):
            reasons.append("UNCERTAIN_ORDER_NO_RETRY")
        commissions = sum((money(self.state["commissions"][exec_id]["amount"])
                           for exec_id, execution in self.state["executions"].items()
                           if execution["active"] and exec_id in self.state["commissions"]), Decimal(0))
        return {"mode": "PAPER BROKER INFRASTRUCTURE / OFFLINE ONLY", "executable": False,
                "submission_enabled": False, "paper_session_verified": verified,
                "verification_origin": "OFFLINE_FIXTURE" if verified else None,
                "generation": self.state["generation"], "positions": self.positions(),
                "intents": deepcopy(self.state["intents"]), "commission_total": str(commissions),
                "recovery_required": bool(reasons), "block_reasons": reasons,
                "quarantined_evidence_count": len(self.state["faults"])}
