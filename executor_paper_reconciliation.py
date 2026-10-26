"""Complete-cycle comparison against the durable ledger; never an order permit."""

from dataclasses import asdict
import hashlib

from executor_broker_snapshot import BROKER_COMPLETION_COHERENCE_SECONDS
from executor_paper_types import (
    BrokerOrderRecord, COMPONENTS, PaperBrokerError, PaperReconciliationSnapshot,
    PositionRecord, TERMINAL, WORKING,
)
from executor_paper_broker import canonical, integer, utc
from simulation_evidence import (
    EvidenceError, MAX_AGE_SECONDS, VerifiedBrokerSnapshot, validate_broker_snapshot,
)


def reconcile_paper_snapshot(ledger, snapshot):
    """Require coherent full history and explicit terminal-order evidence.

    Empty open orders never prove an uncertain order was not submitted. Fills
    come from executions, not orderStatus. Cancellations release reservations
    only after positions, execution history, commissions, and orders agree.
    """
    return _reconcile_snapshot(ledger, snapshot, max_positions=1)


def _reconcile_snapshot(ledger, snapshot, max_positions=1, order_policy=None,authoritative_order_proof=False):
    """Shared rules; only the isolated offline portfolio supplies five positions."""
    if type(max_positions) is not int or max_positions not in (1, 5):
        raise PaperBrokerError("INVALID_RECONCILIATION_POSITION_BOUND")
    ledger.reconciled_at = None
    result = {"executable": False, "submission_enabled": False, "matched": False,
              "block_reasons": [], "accounting_complete": False}
    try:
        if not isinstance(snapshot, PaperReconciliationSnapshot):
            raise PaperBrokerError("COMPLETE_PAPER_SNAPSHOT_REQUIRED")
        now = ledger._session(snapshot.generation)
        state = ledger.state
        if state["faults"]:
            raise PaperBrokerError("QUARANTINED_EVIDENCE_REQUIRES_REVIEW")
        if (snapshot.account != ledger.approval.account
                or snapshot.scope != "ALL_APPROVED_ACCOUNT_INTENTS"
                or not integer(snapshot.cycle_id, 1) or snapshot.cycle_id <= state["last_cycle"]
                or not isinstance(snapshot.complete_components, tuple)
                or len(snapshot.complete_components) != len(COMPONENTS)
                or set(snapshot.complete_components) != set(COMPONENTS)
                or not all(isinstance(value, tuple) for value in
                           (snapshot.positions, snapshot.open_orders, snapshot.completed_orders,
                            snapshot.execution_ids, snapshot.component_receipts))):
            raise PaperBrokerError("SNAPSHOT_IDENTITY_COMPLETENESS_OR_SCOPE_INVALID")
        if len(snapshot.component_receipts) != len(COMPONENTS):
            raise PaperBrokerError("SNAPSHOT_COMPONENT_RECEIPTS_INCOMPLETE")
        receipts = dict(snapshot.component_receipts)
        if (set(receipts) != set(COMPONENTS)
                or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                       or not 0 <= now-value <= MAX_AGE_SECONDS for value in receipts.values())
                or max(receipts.values())-min(receipts.values()) > BROKER_COMPLETION_COHERENCE_SECONDS):
            raise PaperBrokerError("SNAPSHOT_STALE_INCOHERENT_OR_CLOCK_INVALID")
        if any(utc(snapshot.history_start) > utc_from_string(intent["created_at"])
               for intent in state["intents"].values()):
            raise PaperBrokerError("EXECUTION_HISTORY_DOES_NOT_COVER_INTENTS")
        if (len(set(snapshot.execution_ids)) != len(snapshot.execution_ids)
                or set(snapshot.execution_ids) != set(state["executions"])):
            raise PaperBrokerError("EXECUTION_HISTORY_MISSING_OR_UNREPLAYED")
        active_execs = {exec_id for exec_id, item in state["executions"].items() if item["active"]}
        if (active_execs - set(state["commissions"])
                or set(state["commissions"]) - set(state["executions"])):
            raise PaperBrokerError("COMMISSIONS_MISSING_OR_ORPHANED")
        result["accounting_complete"] = True
        expected = ledger.positions()
        observed = {}
        contracts = {intent["contract"]["con_id"]: intent["contract"] for intent in state["intents"].values()}
        for position in snapshot.positions:
            if not isinstance(position, PositionRecord) or position.account != ledger.approval.account:
                raise PaperBrokerError("POSITION_ACCOUNT_OR_TYPE_INVALID")
            if not integer(position.quantity, 0, 25):
                ledger._fault("SHORT_OR_EXCESS_BROKER_POSITION", {"quantity": str(position.quantity)})
            con_id = position.contract.con_id
            if con_id in observed or contracts.get(con_id) != asdict(position.contract):
                raise PaperBrokerError("UNMANAGED_OR_CONTRADICTORY_POSITION")
            observed[con_id] = position.quantity
        observed = {con_id: qty for con_id, qty in observed.items() if qty}
        if observed != expected or len(observed) > max_positions:
            raise PaperBrokerError("BROKER_EXECUTION_POSITION_DISAGREEMENT")
        # Reuse the existing account, quantity, conId and freshness validator.
        for con_id, qty in (observed.items() if observed else ((None, 0),)):
            verified = VerifiedBrokerSnapshot(ledger.approval.account, ledger.approval.account,
                                              ledger.session.managed_accounts, True, qty, con_id,
                                              len(snapshot.open_orders), min(receipts.values()))
            validate_broker_snapshot(verified, ledger.approval.account, now)
        orders = {};order_proofs=[];snapshot_perm_ids=set()
        for terminal, records in ((False, snapshot.open_orders), (True, snapshot.completed_orders)):
            for record in records:
                if not isinstance(record, BrokerOrderRecord):
                    raise PaperBrokerError("INVALID_BROKER_ORDER_SNAPSHOT")
                intent = next((item for item in state["intents"].values()
                               if item["order_ref"] == record.order_ref), None)
                if intent is None or intent["intent_id"] in orders:
                    raise PaperBrokerError("UNMANAGED_OR_DUPLICATED_BROKER_ORDER")
                data = asdict(record)
                if order_policy is not None:
                    order_policy(record, intent)
                if authoritative_order_proof and (not integer(record.perm_id,1) or any(
                    other is not intent and other.get('perm_id')==record.perm_id for other in state['intents'].values())):
                    raise PaperBrokerError('SNAPSHOT_PERMANENT_ID_INVALID_OR_REUSED')
                if authoritative_order_proof and record.perm_id in snapshot_perm_ids:
                    raise PaperBrokerError('SNAPSHOT_PERMANENT_ID_INVALID_OR_REUSED')
                snapshot_perm_ids.add(record.perm_id)
                if any(data[key] != intent[key] for key in
                       ("order_ref", "order_id", "perm_id", "client_id", "contract", "side", "quantity")):
                    if not (authoritative_order_proof and intent['perm_id'] is None and all(data[key]==intent[key] for key in
                        ('order_ref','order_id','client_id','contract','side','quantity'))):
                        raise PaperBrokerError("SNAPSHOT_ORDER_IDENTITY_MISMATCH")
                if intent.get("purpose") == "PROTECTIVE_STOP":
                    from executor_paper_broker import money
                    if record.order_type != "STP" or money(record.stop_price, True) != money(intent["stop_price"], True):
                        raise PaperBrokerError("PROTECTIVE_STOP_SNAPSHOT_MISMATCH")
                if (data["account"] != ledger.approval.account
                        or not integer(record.filled, 0, record.quantity)
                        or not integer(record.remaining, 0, record.quantity)
                        or record.filled != ledger.filled(intent)
                        or not authoritative_order_proof and record.filled != intent["reported_filled"]
                        or record.filled + record.remaining != record.quantity
                        or not authoritative_order_proof and record.status != intent["status"]
                        or (terminal and record.status not in TERMINAL)
                        or (not terminal and record.status not in WORKING)
                        or record.status == "Filled" and record.remaining):
                    raise PaperBrokerError("ORDER_STATUS_EXECUTION_OR_TERMINAL_DISAGREEMENT")
                orders[intent["intent_id"]] = terminal
                if authoritative_order_proof and (record.status!=intent['status'] or record.filled!=intent['reported_filled']
                    or intent['perm_id'] is None or intent.get('evidence_generation')!=snapshot.generation):
                    order_proofs.append(data)
        for intent_id, intent in state["intents"].items():
            if intent["order_id"] is not None and intent_id not in orders:
                raise PaperBrokerError("UNCERTAIN_ABSENCE_IS_NOT_NON_SUBMISSION_PROOF")
        for con_id in contracts:
            remaining_sells = sum(intent["quantity"]-ledger.filled(intent)
                                  for intent_id, intent in state["intents"].items()
                                  if intent["side"] == "SELL" and intent["contract"]["con_id"] == con_id
                                  and intent["status"] != "LOCAL_ABANDONED" and not orders.get(intent_id, False))
            if remaining_sells > expected.get(con_id, 0):
                raise PaperBrokerError("EXIT_RESERVATIONS_EXCEED_OWNED_POSITION")
        fingerprint = asdict(snapshot)
        fingerprint["history_start"] = utc(snapshot.history_start).isoformat()
        digest = hashlib.sha256(canonical(fingerprint).encode()).hexdigest()
        # Executions remain economic truth. A validated full snapshot may supply
        # missing/out-of-order orderStatus evidence; it cannot manufacture fills.
        # Persist it BEFORE the terminal reconciliation commit. A crash in this
        # gap leaves terminal_proven false and reservations held.
        for record in order_proofs:
            ledger._append('snapshot-order:{}:{}:{}'.format(snapshot.generation,snapshot.cycle_id,record['order_ref']),
                'PORT_NATIVE',dict(op='ORDER_PROOF',record=record,snapshot_digest=digest))
        ledger._append("reconcile:{}:{}".format(snapshot.generation, snapshot.cycle_id), "RECONCILED",
                       {"cycle_id": snapshot.cycle_id, "orders": orders, "snapshot_digest": digest})
        ledger.reconciled_at = min(receipts.values())
        result["matched"] = True
    except (PaperBrokerError, EvidenceError, TypeError, ValueError, AttributeError) as exc:
        result["block_reasons"].append(str(exc))
    return result


def utc_from_string(value):
    from datetime import datetime
    return utc(datetime.fromisoformat(value))
