"""Offline MORTIFICATIO risk-budget calculator.

This is a research/reporting utility only. It is not imported by production or
shadow entry decisions and has no broker capability. No daily-loss ceiling is
assumed; callers must supply one to evaluate eligibility.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_CONTRACTS = 25
OPTION_MULTIPLIER = Decimal("100")

@dataclass(frozen=True)
class ExitScenario:
    name: str
    exit_bid: object
    entry_slippage: object = 0
    exit_slippage: object = 0

@dataclass(frozen=True)
class BudgetInput:
    account_cash: object
    entry_ask: object
    contracts: int
    scenarios: tuple
    proposed_daily_ceiling: object = None


def _positive(value, label):
    try: result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc: raise ValueError(label + " is malformed") from exc
    if not result.is_finite() or result <= 0: raise ValueError(label + " must be finite and positive")
    return result

def _nonnegative(value, label):
    try: result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc: raise ValueError(label + " is malformed") from exc
    if not result.is_finite() or result < 0: raise ValueError(label + " must be finite and non-negative")
    return result

def calculate_budget(inputs):
    """Return hypothetical losses; never authorizes an entry."""
    if not isinstance(inputs, BudgetInput): raise ValueError("inputs are required")
    cash = _nonnegative(inputs.account_cash, "account cash")
    ask = _positive(inputs.entry_ask, "entry ask")
    if not isinstance(inputs.contracts, int) or isinstance(inputs.contracts, bool) or not 1 <= inputs.contracts <= MAX_CONTRACTS:
        raise ValueError("contracts must be a whole number from 1 through 25")
    if not isinstance(inputs.scenarios, (tuple, list)) or not inputs.scenarios:
        raise ValueError("at least one exit scenario is required")
    notional = ask * OPTION_MULTIPLIER * inputs.contracts
    rows = []
    for scenario in inputs.scenarios:
        if not isinstance(scenario, ExitScenario) or not isinstance(scenario.name, str) or not scenario.name:
            raise ValueError("exit scenario is malformed")
        bid = _nonnegative(scenario.exit_bid, "exit bid")
        entry_slip = _nonnegative(scenario.entry_slippage, "entry slippage")
        exit_slip = _nonnegative(scenario.exit_slippage, "exit slippage")
        effective_entry = ask + entry_slip
        effective_exit = bid - exit_slip
        pnl = (effective_exit - effective_entry) * OPTION_MULTIPLIER * inputs.contracts
        loss = max(Decimal("0"), -pnl)
        rows.append({"name": scenario.name, "hypothetical_pnl": str(pnl), "dollar_loss": str(loss),
                     "one_trade_loss": str(loss), "two_trade_loss": str(loss * 2),
                     "entry_slippage": str(entry_slip), "exit_slippage": str(exit_slip)})
    ceiling = None if inputs.proposed_daily_ceiling is None else _positive(inputs.proposed_daily_ceiling, "proposed daily ceiling")
    for row in rows:
        row["ceiling_status"] = "UNSET" if ceiling is None else ("ELIGIBLE" if Decimal(row["two_trade_loss"]) <= ceiling else "BLOCKED")
    return {"account_cash": str(cash), "entry_ask": str(ask), "contracts": inputs.contracts,
            "entry_notional": str(notional), "cash_sufficient": cash >= notional,
            "proposed_daily_ceiling": None if ceiling is None else str(ceiling),
            "scenarios": rows, "executable": False}
