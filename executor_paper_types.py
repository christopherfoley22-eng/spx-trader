"""Normalized, offline-only paper broker evidence. No transport or order API."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from simulation_evidence import VerifiedOptionContract


class PaperBrokerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovedPaperAccount:
    account: str
    verification_ref: str
    approved_until: datetime
    host: str = "127.0.0.1"
    port: int = 7497


@dataclass(frozen=True)
class PaperSessionEvidence:
    account: str
    managed_accounts: Tuple[str, ...]
    verification_ref: str
    nonce: str
    verified_at: datetime
    expires_at: datetime
    client_id: int
    mode: str = "PAPER"
    host: str = "127.0.0.1"
    port: int = 7497
    read_only: bool = True
    origin: str = "OFFLINE_FIXTURE"


@dataclass(frozen=True)
class BrokerOrderRecord:
    account: str
    order_ref: str
    order_id: int
    perm_id: int
    client_id: int
    contract: VerifiedOptionContract
    side: str
    quantity: int
    filled: int
    remaining: int
    status: str
    order_type: str = "UNSPECIFIED"
    stop_price: object = None


@dataclass(frozen=True)
class ExecutionRecord:
    account: str
    exec_id: str
    order_ref: str
    order_id: int
    perm_id: int
    client_id: int
    contract: VerifiedOptionContract
    side: str
    quantity: int
    price: object
    correction_of: Optional[str] = None
    bust: bool = False


@dataclass(frozen=True)
class CommissionRecord:
    report_id: str
    exec_id: str
    amount: object
    currency: str = "USD"
    correction_of: Optional[str] = None


@dataclass(frozen=True)
class PositionRecord:
    account: str
    contract: VerifiedOptionContract
    quantity: int


@dataclass(frozen=True)
class PaperReconciliationSnapshot:
    generation: int
    cycle_id: int
    account: str
    positions: Tuple[PositionRecord, ...]
    open_orders: Tuple[BrokerOrderRecord, ...]
    completed_orders: Tuple[BrokerOrderRecord, ...]
    execution_ids: Tuple[str, ...]
    component_receipts: Tuple[Tuple[str, float], ...]
    history_start: datetime
    complete_components: Tuple[str, ...]
    scope: str = "ALL_APPROVED_ACCOUNT_INTENTS"


COMPONENTS = ("POSITIONS", "OPEN_ORDERS", "COMPLETED_ORDERS", "EXECUTIONS", "COMMISSIONS")
WORKING = frozenset(("ApiPending", "PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"))
TERMINAL = frozenset(("Cancelled", "ApiCancelled", "Filled"))
