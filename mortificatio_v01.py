"""
MORTIFICATIO v0.1 integrated DRY-RUN engine.

Purpose:
    Integrate the safety components into one lifecycle.

IMPORTANT:
    - NO IBKR connection.
    - NO order submission.
    - NO market prediction.
    - User supplies CALL or PUT.
    - Broker behavior is simulated.
    - Python 3.9 compatible.

Lifecycle:

    FLAT
      -> entry planning
      -> persistent authorization
      -> simulated entry chunks
      -> broker reconciliation
      -> OPEN
      -> durable strategy monitoring
      -> irreversible exit
      -> simulated partial exits
      -> broker-confirmed FLAT
"""

from dataclasses import dataclass
from typing import Iterable, List, Optional

from mortificatio_entry import (
    EntryLifecycle,
    chunk_plan,
)

from mortificatio_state import (
    MortificatioState,
    MortificatioStateError,
    BrokerSnapshot,
    FLAT,
    ENTERING,
    OPEN,
    EXITING,
    MAX_CONTRACTS,
)

from mortificatio_strategy_state import (
    DurableStrategyState,
    StrategyStateError,
)


OPTION_MULTIPLIER = 100
DEFAULT_ENTRY_CHUNK = 10
DEFAULT_EXIT_CHUNK = 10


class MortificatioV01Error(RuntimeError):
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
    chunks: List[int]


@dataclass
class SimulatedBroker:
    """
    Broker truth for DRY-RUN testing only.

    This intentionally behaves like an external source of truth.
    MORTIFICATIO must reconcile against it rather than assuming
    local state is correct.
    """

    position_qty: int = 0
    con_id: Optional[int] = None
    open_order_count: int = 0
    complete: bool = True

    def snapshot(self):
        return BrokerSnapshot(
            complete=self.complete,
            position_qty=self.position_qty,
            con_id=self.con_id,
            open_order_count=self.open_order_count,
        )

    def simulate_entry_fill(self, con_id, quantity):
        if not isinstance(con_id, int) or con_id <= 0:
            raise MortificatioV01Error(
                "Simulated broker received invalid conId"
            )

        if not isinstance(quantity, int) or quantity < 1:
            raise MortificatioV01Error(
                "Simulated broker received invalid entry quantity"
            )

        if self.position_qty == 0:
            self.con_id = con_id
        elif self.con_id != con_id:
            raise MortificatioV01Error(
                "Cannot mix contracts in simulated broker position"
            )

        if self.position_qty + quantity > MAX_CONTRACTS:
            raise MortificatioV01Error(
                "Simulated broker position would exceed 25"
            )

        self.position_qty += quantity

    def simulate_exit_fill(self, con_id, quantity):
        if self.position_qty <= 0:
            raise MortificatioV01Error(
                "Cannot exit nonexistent simulated position"
            )

        if con_id != self.con_id:
            raise MortificatioV01Error(
                "Exit attempted against wrong simulated contract"
            )

        if not isinstance(quantity, int) or quantity < 1:
            raise MortificatioV01Error(
                "Invalid simulated exit quantity"
            )

        if quantity > self.position_qty:
            raise MortificatioV01Error(
                "Simulated exit would oversell broker position"
            )

        self.position_qty -= quantity

        if self.position_qty == 0:
            self.con_id = None


def max_affordable_contracts(usable_funds, ask):
    if usable_funds <= 0 or ask <= 0:
        return 0

    cost = ask * OPTION_MULTIPLIER
    quantity = int(usable_funds // cost)

    return max(
        0,
        min(quantity, MAX_CONTRACTS),
    )


def build_entry_plan(
    direction,
    spx_price,
    usable_funds,
    candidates: Iterable[OptionQuote],
    max_entry_chunk=DEFAULT_ENTRY_CHUNK,
):
    """
    Direction comes exclusively from the user.

    MORTIFICATIO does NOT predict CALL vs PUT.

    Contract selection:
      1. valid contract
      2. affordable for at least one contract
      3. closest strike to current SPX
      4. lower strike wins an exact distance tie only so behavior
         is deterministic
    """

    if direction not in {"CALL", "PUT"}:
        raise MortificatioV01Error(
            "Direction must be CALL or PUT"
        )

    if spx_price <= 0:
        raise MortificatioV01Error(
            "SPX price must be positive"
        )

    if usable_funds <= 0:
        raise MortificatioV01Error(
            "Usable funds must be positive"
        )

    valid = []

    for candidate in candidates:
        if (
            not isinstance(candidate.con_id, int)
            or candidate.con_id <= 0
        ):
            continue

        if candidate.strike <= 0 or candidate.ask <= 0:
            continue

        quantity = max_affordable_contracts(
            usable_funds,
            candidate.ask,
        )

        if quantity < 1:
            continue

        valid.append(
            (
                abs(candidate.strike - spx_price),
                candidate.strike,
                candidate,
                quantity,
            )
        )

    if not valid:
        raise MortificatioV01Error(
            "No affordable valid option contract"
        )

    valid.sort(
        key=lambda item: (
            item[0],
            item[1],
        )
    )

    _, _, selected, quantity = valid[0]

    return EntryPlan(
        direction=direction,
        spx_price=float(spx_price),
        con_id=selected.con_id,
        strike=float(selected.strike),
        ask=float(selected.ask),
        quantity=quantity,
        estimated_cost=(
            quantity
            * selected.ask
            * OPTION_MULTIPLIER
        ),
        chunks=chunk_plan(
            quantity,
            max_chunk=max_entry_chunk,
        ),
    )


def exit_chunks(quantity, max_chunk=DEFAULT_EXIT_CHUNK):
    if (
        not isinstance(quantity, int)
        or quantity < 1
        or quantity > MAX_CONTRACTS
    ):
        raise MortificatioV01Error(
            "Invalid exit quantity"
        )

    if (
        not isinstance(max_chunk, int)
        or max_chunk < 1
    ):
        raise MortificatioV01Error(
            "Invalid exit chunk size"
        )

    chunks = []
    remaining = quantity

    while remaining:
        chunk = min(remaining, max_chunk)
        chunks.append(chunk)
        remaining -= chunk

    return chunks


class MortificatioV01:
    """
    Integrated dry-run coordinator.

    Lifecycle database and strategy database are deliberately
    separate files for this version.

    Broker remains the final authority for actual position truth.
    """

    def __init__(
        self,
        lifecycle_db,
        strategy_db,
        broker,
    ):
        if not isinstance(broker, SimulatedBroker):
            raise MortificatioV01Error(
                "v0.1 requires SimulatedBroker"
            )

        self.lifecycle = MortificatioState(
            lifecycle_db
        )

        self.strategy = DurableStrategyState(
            strategy_db
        )

        self.broker = broker

    def close(self):
        self.lifecycle.close()
        self.strategy.close()

    def status(self):
        return {
            "lifecycle": self.lifecycle.status(),
            "strategy": self.strategy.status(),
            "broker": self.broker.snapshot(),
        }

    def authorize_entry(
        self,
        direction,
        spx_price,
        usable_funds,
        candidates,
        entry_allowed=True,
        max_entry_chunk=DEFAULT_ENTRY_CHUNK,
    ):
        """
        CALL/PUT authorization boundary.

        entry_allowed is the future insertion point for licensing.

        Revocation/authorization belongs HERE.

        It must never be placed inside open-position risk
        management.
        """

        if not entry_allowed:
            raise MortificatioV01Error(
                "New entries are disabled"
            )

        if self.strategy.status().active:
            raise MortificatioV01Error(
                "Strategy already active"
            )

        plan = build_entry_plan(
            direction=direction,
            spx_price=spx_price,
            usable_funds=usable_funds,
            candidates=candidates,
            max_entry_chunk=max_entry_chunk,
        )

        self.lifecycle.request_entry(
            direction=plan.direction,
            planned_qty=plan.quantity,
            planned_con_id=plan.con_id,
            broker=self.broker.snapshot(),
        )

        return plan

    def simulate_entry(
        self,
        plan,
        fill_quantities=None,
    ):
        """
        Simulates acquisition.

        fill_quantities allows attack tests to model partial fills.

        Example:
            planned 14
            fill_quantities=[10, 2]

        Broker ends with 12.
        MORTIFICATIO then manages the REAL 12 rather than pretending
        the intended 14 were obtained.
        """

        current = self.lifecycle.status()

        if current.state != ENTERING:
            raise MortificatioV01Error(
                "Entry simulation requires ENTERING"
            )

        if current.con_id != plan.con_id:
            raise MortificatioV01Error(
                "Plan contract differs from persistent authorization"
            )

        if current.planned_quantity != plan.quantity:
            raise MortificatioV01Error(
                "Plan quantity differs from persistent authorization"
            )

        entry = EntryLifecycle(
            planned_qty=plan.quantity,
            con_id=plan.con_id,
        )

        if fill_quantities is None:
            fill_quantities = list(plan.chunks)

        total_requested_fill = sum(fill_quantities)

        if total_requested_fill > plan.quantity:
            raise MortificatioV01Error(
                "Simulated fills exceed authorized quantity"
            )

        for quantity in fill_quantities:
            if quantity < 1:
                raise MortificatioV01Error(
                    "Invalid simulated fill"
                )

            entry.record_fill(
                plan.con_id,
                quantity,
            )

            self.broker.simulate_entry_fill(
                plan.con_id,
                quantity,
            )

        if not entry.has_position:
            raise MortificatioV01Error(
                "No simulated position was acquired"
            )

        if entry.remaining_qty > 0:
            entry.stop_entry()

        opened = self.lifecycle.recover_entry(
            self.broker.snapshot()
        )

        if opened.state != OPEN:
            raise MortificatioV01Error(
                "Persistent lifecycle failed to become OPEN"
            )

        if opened.quantity != self.broker.position_qty:
            raise MortificatioV01Error(
                "OPEN quantity differs from broker truth"
            )

        self.strategy.activate(
            direction=opened.direction,
            con_id=opened.con_id,
            entry_spx=plan.spx_price,
        )

        return opened

    def process_spx(self, spx_price):
        """
        Processes one observed SPX price.

        If strategy says EXIT, lifecycle immediately becomes
        irreversible EXITING.

        This method DOES NOT execute an exit fill.
        """

        lifecycle = self.lifecycle.status()
        strategy = self.strategy.status()

        if lifecycle.state == EXITING:
            return {
                "action": "EXITING",
                "reason": strategy.exit_reason,
            }

        if lifecycle.state != OPEN:
            raise MortificatioV01Error(
                "SPX monitoring requires OPEN lifecycle"
            )

        if not strategy.active:
            raise MortificatioV01Error(
                "OPEN lifecycle missing active strategy"
            )

        if (
            strategy.con_id != lifecycle.con_id
            or strategy.direction != lifecycle.direction
        ):
            raise MortificatioV01Error(
                "Strategy/lifecycle identity mismatch"
            )

        decision = self.strategy.process_spx(
            spx_price
        )

        if decision.action == "EXIT":
            self.lifecycle.begin_exit(
                self.broker.snapshot()
            )

            return {
                "action": "EXITING",
                "reason": decision.reason,
                "favorable_points": decision.favorable_points,
                "best_favorable_points": (
                    decision.best_favorable_points
                ),
                "reversal_points": decision.reversal_points,
            }

        return {
            "action": "HOLD",
            "reason": None,
            "favorable_points": decision.favorable_points,
            "best_favorable_points": (
                decision.best_favorable_points
            ),
            "reversal_points": decision.reversal_points,
        }

    def remaining_exit_plan(
        self,
        max_exit_chunk=DEFAULT_EXIT_CHUNK,
    ):
        """
        Reconcile EXITING against broker truth.

        Returns remaining chunks or None when broker is proven flat.

        It never assumes a local exit succeeded.
        """

        lifecycle = self.lifecycle.status()

        if lifecycle.state != EXITING:
            raise MortificatioV01Error(
                "Exit reconciliation requires EXITING"
            )

        broker = self.broker.snapshot()

        if not broker.complete:
            raise MortificatioV01Error(
                "Broker snapshot incomplete"
            )

        if broker.open_order_count != 0:
            raise MortificatioV01Error(
                "Unresolved broker order blocks additional exit"
            )

        if broker.position_qty == 0:
            if broker.con_id is not None:
                raise MortificatioV01Error(
                    "Zero broker position contains conId"
                )

            self.lifecycle.confirm_flat(
                broker
            )

            # Strategy may only be cleared AFTER lifecycle has
            # reconciled broker truth to FLAT.
            self.strategy.clear_after_broker_flat()

            return None

        if broker.con_id != lifecycle.con_id:
            raise MortificatioV01Error(
                "Broker remaining position is wrong contract"
            )

        if broker.position_qty > lifecycle.quantity:
            raise MortificatioV01Error(
                "Broker quantity exceeds original EXITING quantity"
            )

        return exit_chunks(
            broker.position_qty,
            max_chunk=max_exit_chunk,
        )

    def simulate_one_exit_chunk(
        self,
        max_exit_chunk=DEFAULT_EXIT_CHUNK,
    ):
        """
        Simulates ONE exit chunk only.

        Calling one chunk at a time lets attack tests crash/restart
        between every individual fill.
        """

        plan = self.remaining_exit_plan(
            max_exit_chunk=max_exit_chunk,
        )

        if plan is None:
            return None

        quantity = plan[0]

        lifecycle = self.lifecycle.status()

        self.broker.simulate_exit_fill(
            lifecycle.con_id,
            quantity,
        )

        return quantity

    def simulate_exit_to_flat(
        self,
        max_exit_chunk=DEFAULT_EXIT_CHUNK,
    ):
        """
        Convenience method for a normal dry-run.

        Broker truth is checked after every simulated chunk.
        """

        fills = []

        while True:
            plan = self.remaining_exit_plan(
                max_exit_chunk=max_exit_chunk,
            )

            if plan is None:
                return fills

            quantity = plan[0]

            lifecycle = self.lifecycle.status()

            self.broker.simulate_exit_fill(
                lifecycle.con_id,
                quantity,
            )

            fills.append(quantity)
