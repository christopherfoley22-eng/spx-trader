from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional
import math
from safe_sizing import max_affordable_contracts as safe_max_affordable_contracts


# ============================================================
# HARD-CODED EXECUTOR RULES
# ============================================================

MAX_CONTRACTS = 25
OPTION_MULTIPLIER = 100
DAILY_TRADE_LIMIT = 2

INITIAL_STOP = 3.25

NEAR_WINNER_ARM = 4.80
NEAR_WINNER_REVERSAL = 1.00

LET_IT_RIDE_ARM = 5.00
LET_IT_RIDE_TRAIL = 3.00


# ============================================================
# ENUMS
# ============================================================

class Direction(Enum):
    CALL = 1
    PUT = -1


class EngineState(Enum):
    FLAT = auto()
    ENTERING = auto()
    OPEN = auto()
    EXITING = auto()


class StrategyState(Enum):
    INITIAL = auto()
    NEAR_WINNER = auto()
    LET_IT_RIDE = auto()


# ============================================================
# DATA OBJECTS
# ============================================================

@dataclass(frozen=True)
class OptionCandidate:
    con_id: int
    strike: float
    ask: float


@dataclass(frozen=True)
class Selection:
    candidate: OptionCandidate
    quantity: int


@dataclass
class Position:
    direction: Direction
    con_id: int
    strike: float
    quantity: int
    entry_spx: float

    strategy_state: StrategyState = StrategyState.INITIAL
    max_favorable: float = 0.0


# ============================================================
# CONTRACT SELECTION / SIZING
# ============================================================

def max_affordable_contracts(
    usable_funds: float,
    ask: float,
) -> int:

    return safe_max_affordable_contracts(usable_funds, ask)


def select_contract(
    spx: float,
    usable_funds: float,
    candidates,
) -> Optional[Selection]:

    if not math.isfinite(spx) or spx <= 0:
        return None

    ordered = sorted(
        (c for c in candidates if isinstance(c.con_id, int) and c.con_id > 0
         and math.isfinite(c.strike) and c.strike > 0),
        key=lambda c: (
            abs(c.strike - spx),
            c.strike,
        ),
    )

    for candidate in ordered:

        qty = max_affordable_contracts(
            usable_funds,
            candidate.ask,
        )

        if qty >= 1:
            return Selection(
                candidate=candidate,
                quantity=qty,
            )

    return None


# ============================================================
# EXECUTOR
# ============================================================

class Executor:

    def __init__(self):

        self.state = EngineState.FLAT
        self.position: Optional[Position] = None
        self._pending_entry = None

        self.trades_today = 0

        # Dry-run event history.
        self.events = []

    # --------------------------------------------------------
    # INTERNAL EVENT LOGGER
    # --------------------------------------------------------

    def _log(self, event, **details):

        record = {
            "event": event,
            **details,
        }

        self.events.append(record)

        return record

    # --------------------------------------------------------
    # ENTRY
    # --------------------------------------------------------

    def request_entry(
        self,
        direction: Direction,
        spx: float,
        usable_funds: float,
        candidates,
        reconciliation_complete: bool,
        broker_position_qty: int,
        broker_open_orders: int,
    ):

        if not isinstance(direction, Direction):
            return self._log("ENTRY_BLOCKED", reason="INVALID_DIRECTION")

        # Fail closed if broker truth is unavailable.
        if not reconciliation_complete:
            return self._log(
                "ENTRY_BLOCKED",
                reason="RECONCILIATION_INCOMPLETE",
            )

        # Local state must prove flat.
        if self.state != EngineState.FLAT:
            return self._log(
                "ENTRY_BLOCKED",
                reason="LOCAL_STATE_NOT_FLAT",
            )

        if self.position is not None:
            return self._log(
                "ENTRY_BLOCKED",
                reason="LOCAL_POSITION_EXISTS",
            )

        # Broker must independently prove flat.
        if broker_position_qty != 0:
            return self._log(
                "ENTRY_BLOCKED",
                reason="IBKR_POSITION_EXISTS",
            )

        if broker_open_orders != 0:
            return self._log(
                "ENTRY_BLOCKED",
                reason="IBKR_OPEN_ORDER_EXISTS",
            )

        # Hard daily limit.
        if self.trades_today >= DAILY_TRADE_LIMIT:
            return self._log(
                "ENTRY_BLOCKED",
                reason="DAILY_TRADE_LIMIT",
            )

        selection = select_contract(
            spx,
            usable_funds,
            candidates,
        )

        if selection is None:
            return self._log(
                "ENTRY_BLOCKED",
                reason="NO_AFFORDABLE_CONTRACT",
            )

        # Reserve the engine before any hypothetical execution.
        self.state = EngineState.ENTERING
        self._pending_entry = (
            direction, selection.candidate.con_id,
            selection.candidate.strike, selection.quantity,
        )

        return self._log(
            "DRY_RUN_ENTRY_REQUEST",
            direction=direction.name,
            con_id=selection.candidate.con_id,
            strike=selection.candidate.strike,
            ask=selection.candidate.ask,
            quantity=selection.quantity,
            spx=spx,
        )

    # --------------------------------------------------------
    # DRY-RUN ENTRY FILL
    # --------------------------------------------------------

    def simulate_entry_fill(
        self,
        direction: Direction,
        con_id: int,
        strike: float,
        quantity: int,
        entry_spx: float,
    ):

        if self.state != EngineState.ENTERING:
            raise RuntimeError(
                "ENTRY FILL RECEIVED WHILE NOT ENTERING"
            )

        if self.position is not None:
            raise RuntimeError(
                "LOCAL POSITION ALREADY EXISTS"
            )

        if self._pending_entry != (direction, con_id, strike, quantity):
            raise RuntimeError("FILL DIFFERS FROM ENTRY REQUEST")

        if not isinstance(direction, Direction) or not math.isfinite(entry_spx) or entry_spx <= 0:
            raise RuntimeError("INVALID ENTRY DATA")

        if quantity < 1 or quantity > MAX_CONTRACTS:
            raise RuntimeError(
                "INVALID ENTRY QUANTITY"
            )

        self.position = Position(
            direction=direction,
            con_id=con_id,
            strike=strike,
            quantity=quantity,
            entry_spx=entry_spx,
        )

        self.state = EngineState.OPEN
        self._pending_entry = None
        self.trades_today += 1

        return self._log(
            "DRY_RUN_ENTRY_FILLED",
            direction=direction.name,
            con_id=con_id,
            strike=strike,
            quantity=quantity,
            entry_spx=entry_spx,
        )

    # --------------------------------------------------------
    # LIVE PRICE UPDATE
    # --------------------------------------------------------

    def update_spx(self, spx: float):

        if self.state != EngineState.OPEN:
            return None

        if self.position is None:
            raise RuntimeError(
                "OPEN STATE WITHOUT POSITION"
            )

        p = self.position

        if not math.isfinite(spx) or spx <= 0:
            raise RuntimeError("INVALID SPX PRICE")

        move = (
            (spx - p.entry_spx)
            * p.direction.value
        )

        if move > p.max_favorable:
            p.max_favorable = move

        # +5 has priority.
        if (
            p.strategy_state
            in (
                StrategyState.INITIAL,
                StrategyState.NEAR_WINNER,
            )
            and p.max_favorable >= LET_IT_RIDE_ARM
        ):
            p.strategy_state = StrategyState.LET_IT_RIDE

            self._log(
                "LET_IT_RIDE_ARMED",
                peak=p.max_favorable,
                spx=spx,
            )

        elif (
            p.strategy_state == StrategyState.INITIAL
            and p.max_favorable >= NEAR_WINNER_ARM
        ):
            p.strategy_state = StrategyState.NEAR_WINNER

            self._log(
                "NEAR_WINNER_ARMED",
                peak=p.max_favorable,
                spx=spx,
            )

        # ----------------------------------------------------
        # EXIT DECISION
        # ----------------------------------------------------

        if p.strategy_state == StrategyState.INITIAL:

            if move <= -INITIAL_STOP:
                return self._begin_exit(
                    reason="INITIAL_STOP",
                    spx=spx,
                    move=move,
                )

        elif p.strategy_state == StrategyState.NEAR_WINNER:

            if p.max_favorable - move >= NEAR_WINNER_REVERSAL:
                return self._begin_exit(
                    reason="NEAR_WINNER_REVERSAL",
                    spx=spx,
                    move=move,
                )

        elif p.strategy_state == StrategyState.LET_IT_RIDE:

            trailing_floor = (
                p.max_favorable
                - LET_IT_RIDE_TRAIL
            )

            if move <= trailing_floor:
                return self._begin_exit(
                    reason="LET_IT_RIDE_TRAIL",
                    spx=spx,
                    move=move,
                )

        return None

    # --------------------------------------------------------
    # EXIT
    # --------------------------------------------------------

    def _begin_exit(
        self,
        reason: str,
        spx: float,
        move: float,
    ):

        if self.state != EngineState.OPEN:
            return None

        if self.position is None:
            raise RuntimeError(
                "CANNOT EXIT WITHOUT POSITION"
            )

        self.state = EngineState.EXITING

        return self._log(
            "DRY_RUN_EXIT_ALL",
            reason=reason,
            quantity=self.position.quantity,
            con_id=self.position.con_id,
            spx=spx,
            favorable_move=move,
            peak=self.position.max_favorable,
        )

    # --------------------------------------------------------
    # DRY-RUN EXIT FILL
    # --------------------------------------------------------

    def simulate_exit_fill(self):

        if self.state != EngineState.EXITING:
            raise RuntimeError(
                "EXIT FILL RECEIVED WHILE NOT EXITING"
            )

        if self.position is None:
            raise RuntimeError(
                "NO POSITION TO CLOSE"
            )

        closed_qty = self.position.quantity
        closed_con_id = self.position.con_id

        self.position = None
        self.state = EngineState.FLAT

        return self._log(
            "DRY_RUN_FLAT_CONFIRMED",
            quantity=closed_qty,
            con_id=closed_con_id,
        )
