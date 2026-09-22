"""Offline, read-only broker snapshot cycles. No broker client or order API.

Receipt times are local monotonic times for callback coherence, never market
source timestamps. A cycle is evidence for reconciliation, not an order permit.
"""

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import fcntl
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from threading import RLock

from executor_dry_run import DryRunError, DryRunExecutor
from simulation_evidence import EvidenceError, MAX_AGE_SECONDS


VERSION = 1
# Completion spread may use the entire existing one-second freshness budget.
# The connected probe currently completes summary before requesting positions
# and orders. A slower sequence fails closed; this never extends freshness.
BROKER_COMPLETION_COHERENCE_SECONDS = MAX_AGE_SECONDS
COMPONENTS = ("SUMMARY", "POSITIONS", "ORDERS")
SUMMARY_TAGS = ("AvailableFunds", "BuyingPower", "ExcessLiquidity",
                "TotalCashValue", "SettledCash")


class SnapshotSafetyError(RuntimeError):
    pass


def synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return locked


@dataclass(frozen=True)
class Component:
    phase: str = "NOT_STARTED"
    started: float = None
    completed: float = None
    last_receipt: float = None
    items: tuple = ()


@dataclass(frozen=True)
class Cycle:
    cycle_id: str
    generation: int
    account_hash: str
    components: tuple
    invalid: str = None


def _time(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _quantity(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 25


def _con_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _money(value):
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        raise SnapshotSafetyError("ACCOUNT_VALUE_INVALID")
    if not result.is_finite() or result < 0:
        raise SnapshotSafetyError("ACCOUNT_VALUE_INVALID")
    return result


def _local_state(path, account_hash):
    """Reuse the controller's complete state validator on a read-only DB."""
    path = Path(path)
    if not path.is_file():
        raise SnapshotSafetyError("LOCAL_STATE_UNAVAILABLE")
    try:
        db = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
        try:
            db.execute("BEGIN")  # One consistent read-only controller/journal snapshot.
            row = db.execute("SELECT account_hash FROM controller WHERE singleton=1").fetchone()
            if row is None or row[0] != account_hash:
                raise SnapshotSafetyError("LOCAL_ACCOUNT_BINDING_MISMATCH")
            reader = object.__new__(DryRunExecutor)
            reader.db = db
            status = reader.status()
            if status.phase in {"OPEN", "EXITING"}:
                intent = reader._row()[1]
                if db.execute("SELECT 1 FROM audit WHERE event_id=? AND kind='OPEN_CONFIRMED'",
                              ("open:" + intent,)).fetchone() is None:
                    raise SnapshotSafetyError("LOCAL_OPEN_AUDIT_MISSING")
                if status.phase == "EXITING" and db.execute(
                        "SELECT 1 FROM audit WHERE event_id=? AND kind='EXIT_TRIGGERED'",
                        ("exit:" + intent,)).fetchone() is None:
                    raise SnapshotSafetyError("LOCAL_EXIT_AUDIT_MISSING")
            completed = db.execute("SELECT intent_id,trading_day FROM completed_trades").fetchall()
            for intent, _ in completed:
                for event_id, kind in (("flat:" + intent, "CONFIRMED_FLAT"),
                                       ("reconcile:flat:" + intent, "RECONCILED")):
                    if db.execute("SELECT 1 FROM audit WHERE event_id=? AND kind=?",
                                  (event_id, kind)).fetchone() is None:
                        raise SnapshotSafetyError("LOCAL_FLAT_AUDIT_MISSING")
            if status.phase == "FLAT" and sum(day == status.trading_day for _, day in completed) != status.trades_today:
                raise SnapshotSafetyError("LOCAL_DAILY_COMPLETION_MISMATCH")
            return status
        finally:
            db.close()
    except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
        raise SnapshotSafetyError("LOCAL_STATE_CORRUPT") from exc


class ReadOnlySnapshotMachine:
    """One writer; all callbacks identify both generation and immutable cycle ID."""

    def __init__(self, state_path, local_state_path, selected_account=None):
        self.path = Path(state_path)
        self.anchor_path = self.path.with_name(self.path.name + ".generation")
        self.local_state_path = Path(local_state_path)
        if selected_account is not None and (not isinstance(selected_account, str)
                                             or not selected_account.strip()):
            raise SnapshotSafetyError("ACCOUNT_SELECTION_INVALID")
        self.selected_account = selected_account
        self.lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = self.path.with_name(self.path.name + ".lock").open("a+b")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock_file.close()
            raise SnapshotSafetyError("SNAPSHOT_WRITER_ALREADY_ACTIVE") from exc
        self.generation = 0
        self.cycle_serial = 0
        self.connected = False
        self.accounts = None
        self.selected = None
        self.cycle = None
        self.recovery_required = False
        self.block_reason = "NO_CONNECTION"
        self.reconciliation = "BLOCK_NEW_ENTRY"
        self.rejected_callbacks = 0
        self.corrupt = False
        self.account_hash_binding = None
        self.last_monotonic = None
        try:
            if self.path.exists():
                self._load()
                self.block_reason = "RESTART_REQUIRES_NEW_CYCLE"
                self.reconciliation = "BLOCK_NEW_ENTRY"
                self._persist()
            else:
                if self.anchor_path.exists():
                    raise SnapshotSafetyError("GENERATION_ANCHOR_WITHOUT_STATE")
                self._persist()
        except (SnapshotSafetyError, OSError, ValueError, TypeError, KeyError):
            self.corrupt = True
            self.recovery_required = True
            self.block_reason = "PERSISTED_OBSERVATION_STATE_UNVERIFIABLE"
            self.reconciliation = "BLOCK_NEW_ENTRY"

    @synchronized
    def close(self):
        if not self.lock_file.closed:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise SnapshotSafetyError("PERSISTED_OBSERVATION_STATE_UNVERIFIABLE") from exc
        expected = {"version", "generation", "cycle_serial", "account_hash",
                    "block_reason", "reconciliation", "recovery_required", "integrity"}
        if not isinstance(raw, dict) or set(raw) != expected or raw["version"] != VERSION:
            raise SnapshotSafetyError("PERSISTED_OBSERVATION_STATE_UNVERIFIABLE")
        integrity = raw.pop("integrity")
        if integrity != hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest():
            raise SnapshotSafetyError("PERSISTED_OBSERVATION_STATE_UNVERIFIABLE")
        if (not isinstance(raw["generation"], int) or isinstance(raw["generation"], bool)
                or raw["generation"] < 0 or not isinstance(raw["cycle_serial"], int)
                or isinstance(raw["cycle_serial"], bool) or raw["cycle_serial"] < 0
                or raw["reconciliation"] not in {"BLOCK_NEW_ENTRY", "MATCHED_FLAT", "MATCHED_ACTIVE"}
                or not (raw["block_reason"] is None or isinstance(raw["block_reason"], str))
                or not isinstance(raw["recovery_required"], bool)):
            raise SnapshotSafetyError("PERSISTED_OBSERVATION_STATE_UNVERIFIABLE")
        account_hash = raw["account_hash"]
        if account_hash is not None and (not isinstance(account_hash, str)
                                         or len(account_hash) != 64
                                         or any(c not in "0123456789abcdef" for c in account_hash)):
            raise SnapshotSafetyError("PERSISTED_OBSERVATION_STATE_UNVERIFIABLE")
        if self.selected_account is not None and account_hash is not None:
            if hashlib.sha256(self.selected_account.encode()).hexdigest() != account_hash:
                raise SnapshotSafetyError("PERSISTED_ACCOUNT_BINDING_MISMATCH")
        self.generation = raw["generation"]
        self.cycle_serial = raw["cycle_serial"]
        self.account_hash_binding = account_hash
        self.recovery_required = raw["recovery_required"]
        try:
            anchor = json.loads(self.anchor_path.read_text())
            if (not isinstance(anchor, dict) or set(anchor) != {"generation", "cycle_serial"}
                    or not isinstance(anchor["generation"], int)
                    or isinstance(anchor["generation"], bool)
                    or not isinstance(anchor["cycle_serial"], int)
                    or isinstance(anchor["cycle_serial"], bool)
                    or anchor["generation"] < 0 or anchor["cycle_serial"] < 0
                    or anchor["generation"] != self.generation
                    or anchor["cycle_serial"] != self.cycle_serial):
                raise SnapshotSafetyError("GENERATION_ROLLBACK_OR_ANCHOR_INVALID")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise SnapshotSafetyError("GENERATION_ANCHOR_UNVERIFIABLE") from exc

    def _persist(self):
        payload = {"version": VERSION, "generation": self.generation,
                   "cycle_serial": self.cycle_serial, "account_hash": self.account_hash_binding,
                   "block_reason": self.block_reason,
                   "reconciliation": self.reconciliation,
                   "recovery_required": self.recovery_required}
        payload["integrity"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        try:
            self._atomic_json(self.path, payload)
            self._atomic_json(self.anchor_path, {"generation": self.generation,
                                                 "cycle_serial": self.cycle_serial})
        except OSError as exc:
            self.corrupt = True
            self.recovery_required = True
            self.reconciliation = "BLOCK_NEW_ENTRY"
            self.block_reason = "PERSISTENCE_WRITE_FAILED"
            raise SnapshotSafetyError("PERSISTENCE_WRITE_FAILED") from exc

    @staticmethod
    def _atomic_json(path, payload):
        temp = path.with_name(path.name + ".tmp")
        descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp), str(path))
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temp.exists():
                temp.unlink()

    def _block(self, reason, recovery=False):
        self.block_reason = reason
        self.reconciliation = "BLOCK_NEW_ENTRY"
        self.recovery_required = self.recovery_required or recovery
        self._persist()

    def _reject(self, reason, invalidate=True):
        self.rejected_callbacks += 1
        if self.corrupt:
            raise SnapshotSafetyError(reason)
        if invalidate and self.cycle is not None:
            self.cycle = replace(self.cycle, invalid=reason)
            self._block(reason, True)
        else:
            self._block(reason)
        raise SnapshotSafetyError(reason)

    @synchronized
    def connect(self):
        if self.corrupt or self.connected or self.generation >= 2**63 - 1:
            self._reject("GENERATION_UNVERIFIABLE")
        self.generation += 1
        self.connected = True
        self.accounts = self.selected = self.cycle = None
        self.last_monotonic = None
        self.recovery_required = False
        self._block("SNAPSHOT_CYCLE_REQUIRED")
        return self.generation

    @synchronized
    def disconnect(self, generation):
        if generation != self.generation or not self.connected:
            self._reject("OLD_GENERATION_CALLBACK")
        self.connected = False
        self.cycle = None
        self._block("DISCONNECTED", True)

    @synchronized
    def managed_accounts(self, generation, accounts):
        self._generation(generation)
        if (not isinstance(accounts, (tuple, list)) or not accounts
                or any(not isinstance(a, str) or not a.strip() for a in accounts)
                or len(set(accounts)) != len(accounts)):
            self._reject("ACCOUNT_LIST_INVALID")
        accounts = tuple(accounts)
        if self.accounts is not None and self.accounts != accounts:
            self._reject("ACCOUNT_CHANGED")
        self.accounts = accounts
        selected = self.selected_account or (accounts[0] if len(accounts) == 1 else None)
        if selected not in accounts:
            self._block("ACCOUNT_SELECTION_REQUIRED")
            return
        if self.selected is not None and self.selected != selected:
            self._reject("ACCOUNT_CHANGED")
        account_hash = hashlib.sha256(selected.encode()).hexdigest()
        if self.account_hash_binding is not None and self.account_hash_binding != account_hash:
            self._reject("ACCOUNT_CHANGED")
        self.account_hash_binding = account_hash
        self.selected = selected
        if self.cycle is None:
            self._block("SNAPSHOT_CYCLE_REQUIRED")

    def _generation(self, generation):
        if self.corrupt or not isinstance(generation, int) or isinstance(generation, bool):
            self._reject("GENERATION_UNVERIFIABLE", False)
        if generation != self.generation or not self.connected:
            self._reject("OLD_GENERATION_CALLBACK")

    @synchronized
    def begin_cycle(self, generation, now_monotonic):
        self._generation(generation)
        if self.recovery_required:
            self._reject("RECONNECT_REQUIRED")
        if (not _time(now_monotonic) or self.last_monotonic is not None
                and now_monotonic < self.last_monotonic):
            self._reject("CYCLE_TIME_INVALID")
        if self.accounts is None or self.selected is None:
            self._reject("ACCOUNT_SELECTION_REQUIRED")
        if self.cycle is not None:
            if self.cycle.invalid is not None:
                self._reject("RECONNECT_REQUIRED")
            if not all(c.phase == "COMPLETE" for c in self.cycle.components):
                self._reject("PREVIOUS_CYCLE_INCOMPLETE")
        self.cycle_serial += 1
        self.last_monotonic = now_monotonic
        self.cycle = Cycle("g{}-c{}".format(self.generation, self.cycle_serial),
                           self.generation, hashlib.sha256(self.selected.encode()).hexdigest(),
                           (Component(), Component(), Component()))
        self.recovery_required = False
        self._block("SNAPSHOT_COMPONENTS_INCOMPLETE")
        return self.cycle.cycle_id

    def _component(self, generation, cycle_id, name):
        self._generation(generation)
        if self.cycle is None or cycle_id != self.cycle.cycle_id:
            self._reject("OLD_OR_UNKNOWN_CYCLE_CALLBACK")
        if self.cycle.invalid is not None:
            self._reject("CYCLE_INVALID")
        if name not in COMPONENTS:
            self._reject("UNKNOWN_COMPONENT")
        return COMPONENTS.index(name), self.cycle.components[COMPONENTS.index(name)]

    def _put(self, index, component):
        if component.last_receipt is not None:
            if self.last_monotonic is not None and component.last_receipt < self.last_monotonic:
                self._reject("MONOTONIC_CALLBACK_ROLLBACK")
            self.last_monotonic = component.last_receipt
        components = list(self.cycle.components)
        components[index] = component
        self.cycle = replace(self.cycle, components=tuple(components))
        self._block("SNAPSHOT_COMPONENTS_INCOMPLETE")

    @synchronized
    def begin_component(self, generation, cycle_id, name, receipt):
        index, component = self._component(generation, cycle_id, name)
        if not _time(receipt) or component.phase != "NOT_STARTED":
            self._reject("COMPONENT_BEGIN_INVALID")
        self._put(index, Component("STARTED", receipt, None, receipt, ()))

    @synchronized
    def observation(self, generation, cycle_id, name, account, key, value, receipt):
        index, component = self._component(generation, cycle_id, name)
        if component.phase != "STARTED" or not _time(receipt) or receipt < component.last_receipt:
            self._reject("COMPONENT_CALLBACK_OUT_OF_ORDER")
        if account != self.selected or self.cycle.account_hash != hashlib.sha256(self.selected.encode()).hexdigest():
            self._reject("ACCOUNT_CHANGED")
        if name == "SUMMARY":
            if key not in SUMMARY_TAGS or not isinstance(value, tuple) or len(value) != 2 or value[1] != "USD":
                self._reject("ACCOUNT_VALUE_INVALID")
            try:
                normalized = str(_money(value[0]))
            except SnapshotSafetyError:
                self._reject("ACCOUNT_VALUE_INVALID")
        elif name == "POSITIONS":
            if not _con_id(key) or not _quantity(value):
                self._reject("POSITION_INVALID")
            normalized = value
        else:
            if not _con_id(key) or not _con_id(value):
                self._reject("OPEN_ORDER_INVALID")
            normalized = value
        prior = dict(component.items)
        if key in prior and prior[key] != normalized:
            self._reject("CONTRADICTORY_CALLBACK")
        if key not in prior:
            prior[key] = normalized
        self._put(index, replace(component, last_receipt=receipt,
                                 items=tuple(sorted(prior.items()))))

    @synchronized
    def complete_component(self, generation, cycle_id, name, receipt):
        index, component = self._component(generation, cycle_id, name)
        if (component.phase != "STARTED" or not _time(receipt)
                or receipt < component.last_receipt or receipt - component.started > MAX_AGE_SECONDS):
            self._reject("COMPONENT_COMPLETION_INVALID")
        if name == "SUMMARY" and set(dict(component.items)) != set(SUMMARY_TAGS):
            self._reject("ACCOUNT_SUMMARY_INCOMPLETE")
        self._put(index, replace(component, phase="COMPLETE", completed=receipt,
                                 last_receipt=receipt))

    @synchronized
    def status(self, now_monotonic):
        component_state = ({name: self.cycle.components[index].phase
                            for index, name in enumerate(COMPONENTS)}
                           if self.cycle is not None else {name: "NOT_STARTED" for name in COMPONENTS})
        reason = self.block_reason
        reconciliation = "BLOCK_NEW_ENTRY"
        recovery = self.recovery_required
        age = coherence = None
        if self.corrupt:
            reason, recovery = "PERSISTED_OBSERVATION_STATE_UNVERIFIABLE", True
        elif not self.connected:
            reason = "DISCONNECTED" if reason != "RESTART_REQUIRES_NEW_CYCLE" else reason
        elif self.selected is None:
            reason = "ACCOUNT_SELECTION_REQUIRED"
        elif self.cycle is None:
            reason = "SNAPSHOT_CYCLE_REQUIRED"
        elif self.cycle.invalid is not None:
            reason, recovery = self.cycle.invalid, True
        elif not all(c.phase == "COMPLETE" for c in self.cycle.components):
            reason = "SNAPSHOT_COMPONENTS_INCOMPLETE"
        elif not _time(now_monotonic):
            reason, recovery = "MONOTONIC_CLOCK_INVALID", True
        else:
            times = [c.completed for c in self.cycle.components]
            age = now_monotonic - min(times)
            coherence = max(times) - min(times)
            if age < 0 or now_monotonic < max(times) or (self.last_monotonic is not None
                                                         and now_monotonic < self.last_monotonic):
                reason, recovery = "MONOTONIC_CLOCK_ROLLBACK", True
            elif coherence > BROKER_COMPLETION_COHERENCE_SECONDS:
                reason = "SNAPSHOT_INCOHERENT"
            elif age > MAX_AGE_SECONDS:
                reason = "SNAPSHOT_EXPIRED"
            else:
                try:
                    local = _local_state(self.local_state_path, self.cycle.account_hash)
                    positions = [(cid, qty) for cid, qty in self.cycle.components[1].items if qty]
                    orders = self.cycle.components[2].items
                    if len(positions) > 1:
                        raise SnapshotSafetyError("MULTIPLE_BROKER_POSITIONS")
                    broker_qty, broker_con = (positions[0][1], positions[0][0]) if positions else (0, None)
                    if orders:
                        reason = "API_VISIBLE_OPEN_ORDER_PRESENT"
                    elif broker_qty != local.owned or broker_con != local.con_id:
                        reason, recovery = "BROKER_LOCAL_POSITION_DISAGREEMENT", True
                    elif local.phase == "FLAT":
                        reconciliation, reason = "MATCHED_FLAT", None
                    else:
                        reconciliation, reason = "MATCHED_ACTIVE", "ACTIVE_LIFECYCLE"
                except (SnapshotSafetyError, DryRunError, EvidenceError,
                        sqlite3.DatabaseError, ValueError, TypeError) as exc:
                    reason, recovery = "LOCAL_STATE_UNVERIFIABLE: " + str(exc), True
        if recovery:
            reconciliation = "BLOCK_NEW_ENTRY"
            reason = reason or "RECOVERY_REQUIRED_RECONNECT"
        if reconciliation != self.reconciliation or reason != self.block_reason or recovery != self.recovery_required:
            self.reconciliation = reconciliation
            self.block_reason = reason
            self.recovery_required = recovery
            if not self.corrupt:
                self._persist()
        return {"mode": "READ-ONLY BROKER SNAPSHOT / NO ORDERS",
                "generation": self.generation,
                "cycle_id": self.cycle.cycle_id if self.cycle is not None else None,
                "cycle_state": "INVALID" if self.cycle and self.cycle.invalid else
                               "COMPLETE" if self.cycle and all(c.phase == "COMPLETE" for c in self.cycle.components) else
                               "INCOMPLETE" if self.cycle else "NONE",
                "components": component_state,
                "age_seconds": age, "coherence_seconds": coherence,
                "age_status": "FRESH" if age is not None and 0 <= age <= MAX_AGE_SECONDS else "BLOCKED",
                "coherence_status": "COHERENT" if coherence is not None and coherence <= BROKER_COMPLETION_COHERENCE_SECONDS else "BLOCKED",
                "reconciliation": reconciliation, "block_new_entry": reconciliation != "MATCHED_FLAT",
                "block_reasons": [reason] if reason else [],
                "order_visibility": "API_VISIBLE_ONLY",
                "recovery_required": recovery, "executable": False,
                "rejected_callback_count": self.rejected_callbacks}
