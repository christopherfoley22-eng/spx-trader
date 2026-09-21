from dataclasses import dataclass
import math
from safe_sizing import max_affordable_contracts as safe_max_affordable_contracts
from typing import Iterable, Optional

from executor_state import (
    ExecutorState,
    BrokerSnapshot,
    SafetyError,
    MAX_CONTRACTS,
)


OPTION_MULTIPLIER = 100


class SelectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OptionQuote:
    con_id: int
    strike: float
    ask: float


@dataclass(frozen=True)
class EntryPlan:
    direction: str
    spx_price: float
    con_id: int
    strike: float
    ask: float
    quantity: int
    estimated_cost: float


def max_affordable_contracts(
    usable_funds: float,
    ask: float,
) -> int:
    return safe_max_affordable_contracts(usable_funds, ask)


def build_entry_plan(
    direction: str,
    spx_price: float,
    usable_funds: float,
    candidates: Iterable[OptionQuote],
) -> EntryPlan:
    """
    Builds a DRY-RUN entry plan.

    Selection doctrine:
    1. User supplies direction. MORTIFICATIO never predicts it.
    2. Prefer the available strike closest to SPX.
    3. The selected contract must be affordable for >= 1 contract.
    4. Quantity is based on actual supplied usable funds and ask.
    5. Quantity can never exceed 25.

    Exact equal-distance strike ties are resolved by lower strike first
    solely to make behavior deterministic.
    """

    if direction not in {"CALL", "PUT"}:
        raise SelectionError("Direction must be CALL or PUT")

    if not math.isfinite(spx_price) or spx_price <= 0:
        raise SelectionError("SPX price must be positive")

    if not math.isfinite(usable_funds) or usable_funds <= 0:
        raise SelectionError("No usable funds available")

    valid = []

    for c in candidates:
        if not isinstance(c.con_id, int) or c.con_id <= 0:
            continue

        if (
            not math.isfinite(c.strike) or not math.isfinite(c.ask)
            or c.strike <= 0 or c.ask <= 0
        ):
            continue

        qty = max_affordable_contracts(
            usable_funds,
            c.ask,
        )

        if qty >= 1:
            valid.append(
                (
                    abs(c.strike - spx_price),
                    c.strike,
                    c,
                    qty,
                )
            )

    if not valid:
        raise SelectionError(
            "No affordable valid option contract available"
        )

    valid.sort(
        key=lambda item: (
            item[0],  # distance from SPX
            item[1],  # deterministic tie breaker
        )
    )

    _, _, chosen, qty = valid[0]

    return EntryPlan(
        direction=direction,
        spx_price=spx_price,
        con_id=chosen.con_id,
        strike=chosen.strike,
        ask=chosen.ask,
        quantity=qty,
        estimated_cost=qty * chosen.ask * OPTION_MULTIPLIER,
    )


class Mortificatio:
    """
    MORTIFICATIO core coordinator.

    IMPORTANT:
    There is intentionally NO placeOrder() method here.
    This version cannot submit an IBKR order.
    """

    def __init__(self, db_path: str):
        self.state = ExecutorState(db_path)

    def prepare_entry(
        self,
        direction: str,
        spx_price: float,
        usable_funds: float,
        candidates: Iterable[OptionQuote],
        broker_snapshot: BrokerSnapshot,
    ) -> EntryPlan:
        """
        Build an entry plan and reserve the persistent lifecycle.

        If ANY safety check fails, no entry reservation occurs.
        """

        plan = build_entry_plan(
            direction=direction,
            spx_price=spx_price,
            usable_funds=usable_funds,
            candidates=candidates,
        )

        self.state.request_entry(
            direction=plan.direction,
            planned_qty=plan.quantity,
            planned_con_id=plan.con_id,
            broker=broker_snapshot,
        )

        return plan

    def status(self):
        return self.state.status()

    def close(self):
        self.state.close()
