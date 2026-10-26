"""Offline fail-closed risk preflight for MORTIFICATIO research/shadow use.

This module contains no broker integration.  Time cutoffs and holding periods
are deliberately research metadata and are never consulted by production entry.
It is not wired into production Executor or shadow entry decisions; callers must
explicitly invoke this preflight when testing a proposed policy.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_CONTRACTS = 25
MAX_DAILY_TRADES = 2
DEFAULT_DAILY_LOSS_LIMIT = None  # UNSET until the operator chooses a ceiling.
# Existing locked behavior is two filled trades per New York date.  An earlier
# product plan proposed one; this module records the difference without changing
# the enforced production rule.
EXISTING_DAILY_TRADE_LIMIT = 2
EARLIER_PLANNED_DAILY_TRADE_LIMIT = 1

@dataclass(frozen=True)
class RiskState:
    account_binding: str
    active_position_qty: int
    exit_unresolved: bool
    daily_trades: int
    daily_realized_loss: object
    position_consistent: bool = True
    journal_consistent: bool = True

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    executable: bool = False

@dataclass(frozen=True)
class ResearchTimeSettings:
    """Research labels only; never used by production or shadow entry gates."""
    cutoff_new_entries: str = None
    maximum_holding_minutes: int = None

def _money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("daily loss is malformed") from exc
    if not amount.is_finite():
        raise ValueError("daily loss is non-finite")
    return amount

def entry_preflight(state, requested_contracts, selected_account,
                    daily_loss_limit=DEFAULT_DAILY_LOSS_LIMIT):
    """Return a non-executable decision; any uncertain state blocks entry."""
    if not isinstance(state, RiskState) or not isinstance(selected_account, str) or not selected_account:
        return RiskDecision(False, "RISK_EVIDENCE_INCOMPLETE")
    if state.account_binding != selected_account:
        return RiskDecision(False, "ACCOUNT_BINDING_MISMATCH")
    if not state.position_consistent or not state.journal_consistent:
        return RiskDecision(False, "RECOVERY_REQUIRED_INCONSISTENT_STATE")
    if not isinstance(requested_contracts, int) or isinstance(requested_contracts, bool):
        return RiskDecision(False, "QUANTITY_UNCERTAIN")
    if requested_contracts < 1 or requested_contracts > MAX_CONTRACTS:
        return RiskDecision(False, "QUANTITY_LIMIT")
    if state.active_position_qty != 0:
        return RiskDecision(False, "ACTIVE_POSITION")
    if state.exit_unresolved:
        return RiskDecision(False, "EXIT_UNRESOLVED")
    if not isinstance(state.daily_trades, int) or isinstance(state.daily_trades, bool) or state.daily_trades < 0:
        return RiskDecision(False, "DAILY_COUNT_UNCERTAIN")
    if state.daily_trades >= MAX_DAILY_TRADES:
        return RiskDecision(False, "DAILY_TRADE_LIMIT")
    try:
        loss = _money(state.daily_realized_loss)
        limit = _money(daily_loss_limit)
    except ValueError:
        return RiskDecision(False, "DAILY_LOSS_UNCERTAIN")
    if limit <= 0:
        return RiskDecision(False, "DAILY_LOSS_LIMIT_UNCERTAIN")
    if loss > 0:
        return RiskDecision(False, "DAILY_PNL_INCONSISTENT")
    if -loss >= limit:
        return RiskDecision(False, "DAILY_DOLLAR_LOSS_LIMIT")
    return RiskDecision(True, "ELIGIBLE", False)
