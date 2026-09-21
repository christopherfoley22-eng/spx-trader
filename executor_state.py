import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Optional


# HARD-CODED SAFETY LIMIT.
# Changing this requires editing/redeploying source code.
DAILY_TRADE_LIMIT = 2
MAX_CONTRACTS = 25

FLAT = "FLAT"
ENTERING = "ENTERING"
OPEN = "OPEN"
EXITING = "EXITING"

VALID_STATES = {FLAT, ENTERING, OPEN, EXITING}
VALID_DIRECTIONS = {"CALL", "PUT"}

NY = ZoneInfo("America/New_York")


class SafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerSnapshot:
    complete: bool
    position_qty: int
    con_id: Optional[int]
    open_order_count: int


@dataclass(frozen=True)
class ExecutorStatus:
    state: str
    direction: Optional[str]
    quantity: int
    con_id: Optional[int]
    trades_today: int
    trading_day: str


class ExecutorState:
    """
    Persistent safety/state authority.

    IMPORTANT:
    - Contains NO IBKR placeOrder() call.
    - Does NOT submit, cancel, or modify orders.
    - BrokerSnapshot is supplied by the reconciliation layer.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db = sqlite3.connect(
            db_path,
            timeout=5,
            isolation_level=None,
        )
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")

        self._create_schema()
        self._initialize()

    def _create_schema(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS executor_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state TEXT NOT NULL,
                direction TEXT,
                quantity INTEGER NOT NULL,
                con_id INTEGER,
                trades_today INTEGER NOT NULL,
                trading_day TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS event_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

    @staticmethod
    def _now():
        return datetime.now(NY)

    @classmethod
    def _today(cls):
        return cls._now().date().isoformat()

    @classmethod
    def _timestamp(cls):
        return cls._now().isoformat()

    def _initialize(self):
        row = self.db.execute(
            "SELECT id FROM executor_state WHERE id = 1"
        ).fetchone()

        if row is None:
            self.db.execute("""
                INSERT INTO executor_state (
                    id, state, direction, quantity, con_id,
                    trades_today, trading_day, updated_at
                )
                VALUES (1, ?, NULL, 0, NULL, 0, ?, ?)
            """, (FLAT, self._today(), self._timestamp()))

    def _journal(self, event: str, detail: str):
        self.db.execute("""
            INSERT INTO event_journal(event, detail, created_at)
            VALUES (?, ?, ?)
        """, (event, detail, self._timestamp()))

    def _roll_day_if_safe(self):
        today = self._today()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            status = self.status()
            if today < status.trading_day:
                raise SafetyError("New York clock moved backward")
            if today == status.trading_day:
                self.db.execute("COMMIT")
                return
            if status.state != FLAT or status.quantity != 0:
                raise SafetyError(
                    "Trading day changed while Executor is not safely FLAT"
                )
            self.db.execute("""
                UPDATE executor_state
                SET trades_today = 0,
                    trading_day = ?,
                    updated_at = ?
                WHERE id = 1
            """, (today, self._timestamp()))

            self._journal(
                "DAY_ROLLOVER",
                "Daily trade count safely reset while FLAT"
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def status(self) -> ExecutorStatus:
        row = self.db.execute("""
            SELECT state, direction, quantity, con_id,
                   trades_today, trading_day
            FROM executor_state
            WHERE id = 1
        """).fetchone()

        if row is None:
            raise SafetyError("Persistent Executor state is missing")

        state, direction, qty, con_id, trades, day = row

        if state not in VALID_STATES:
            raise SafetyError("Persistent state contains invalid state")

        if not isinstance(qty, int) or qty < 0 or qty > MAX_CONTRACTS:
            raise SafetyError("Persistent quantity is impossible")

        if not isinstance(trades, int) or trades < 0 or trades > DAILY_TRADE_LIMIT:
            raise SafetyError("Persistent daily trade count is impossible")

        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except (TypeError, ValueError):
            raise SafetyError("Persistent trading date is invalid")

        if state == FLAT and (direction is not None or qty != 0 or con_id is not None):
            raise SafetyError("Persistent FLAT state is contradictory")
        if state == ENTERING and (
            direction not in VALID_DIRECTIONS or qty != 0
            or not isinstance(con_id, int) or con_id <= 0
        ):
            raise SafetyError("Persistent ENTERING state is contradictory")
        if state in {OPEN, EXITING} and (
            direction not in VALID_DIRECTIONS or qty < 1
            or not isinstance(con_id, int) or con_id <= 0
        ):
            raise SafetyError("Persistent active state is contradictory")

        return ExecutorStatus(
            state=state,
            direction=direction,
            quantity=qty,
            con_id=con_id,
            trades_today=trades,
            trading_day=day,
        )

    @staticmethod
    def _validate_snapshot(snapshot: BrokerSnapshot):
        if not isinstance(snapshot, BrokerSnapshot) or snapshot.complete is not True:
            raise SafetyError("Broker snapshot incomplete or invalid")
        if (not isinstance(snapshot.position_qty, int)
                or isinstance(snapshot.position_qty, bool)
                or snapshot.position_qty < 0):
            raise SafetyError("Broker snapshot contains negative quantity")

        if snapshot.position_qty > MAX_CONTRACTS:
            raise SafetyError("Broker position exceeds Executor maximum")

        if (not isinstance(snapshot.open_order_count, int)
                or isinstance(snapshot.open_order_count, bool)
                or snapshot.open_order_count < 0):
            raise SafetyError("Invalid broker open-order count")

        if snapshot.position_qty == 0 and snapshot.con_id is not None:
            raise SafetyError("Flat broker snapshot contains conId")
        if snapshot.position_qty > 0 and (
            not isinstance(snapshot.con_id, int)
            or isinstance(snapshot.con_id, bool) or snapshot.con_id <= 0
        ):
            raise SafetyError("Broker position missing conId")

    def request_entry(
        self,
        direction: str,
        planned_qty: int,
        planned_con_id: int,
        broker: BrokerSnapshot,
    ):
        """
        Reserve an entry lifecycle.

        Does NOT submit an order.
        """

        self._roll_day_if_safe()
        self._validate_snapshot(broker)

        if direction not in VALID_DIRECTIONS:
            raise SafetyError("Direction must be CALL or PUT")

        if not isinstance(planned_qty, int):
            raise SafetyError("Quantity must be a whole number")

        if planned_qty < 1 or planned_qty > MAX_CONTRACTS:
            raise SafetyError("Requested quantity outside safety bounds")

        if not isinstance(planned_con_id, int) or planned_con_id <= 0:
            raise SafetyError("Invalid planned conId")

        with self.db:
            current = self.status()

            if current.state != FLAT:
                raise SafetyError("Executor is not FLAT")

            if current.quantity != 0 or current.con_id is not None:
                raise SafetyError("Local FLAT state is contradictory")

            if current.trades_today >= DAILY_TRADE_LIMIT:
                raise SafetyError("Daily trade limit reached")

            if broker.position_qty != 0:
                raise SafetyError("IBKR reports an existing position")

            if broker.con_id is not None:
                raise SafetyError(
                    "Broker flat snapshot contains unexpected conId"
                )

            if broker.open_order_count != 0:
                raise SafetyError("IBKR reports working/open orders")

            self.db.execute("""
                UPDATE executor_state
                SET state = ?,
                    direction = ?,
                    quantity = 0,
                    con_id = ?,
                    updated_at = ?
                WHERE id = 1
            """, (
                ENTERING,
                direction,
                planned_con_id,
                self._timestamp(),
            ))

            self._journal(
                "ENTRY_RESERVED",
                f"{direction} qty={planned_qty} conId={planned_con_id}"
            )

    def confirm_entry_fill(
        self,
        filled_qty: int,
        con_id: int,
        broker: BrokerSnapshot,
    ):
        """
        Transition ENTERING -> OPEN only after broker truth confirms
        the actual filled position.

        The daily trade is counted here: at least one contract actually
        exists at the broker.
        """

        self._validate_snapshot(broker)

        if not isinstance(filled_qty, int):
            raise SafetyError("Filled quantity must be whole")

        if filled_qty < 1 or filled_qty > MAX_CONTRACTS:
            raise SafetyError("Filled quantity outside safety bounds")

        with self.db:
            current = self.status()

            if current.state != ENTERING:
                raise SafetyError("Executor is not ENTERING")

            if current.trading_day != self._today():
                raise SafetyError(
                    "Entry fill date cannot be proven from broker snapshot"
                )

            if current.con_id != con_id:
                raise SafetyError("Fill conId differs from reserved contract")

            if broker.position_qty != filled_qty:
                raise SafetyError(
                    "Local fill quantity disagrees with broker position"
                )

            if broker.con_id != con_id:
                raise SafetyError(
                    "Broker conId disagrees with reserved contract"
                )

            if broker.open_order_count != 0:
                raise SafetyError(
                    "Entry cannot be finalized with unresolved open orders"
                )

            if current.trades_today >= DAILY_TRADE_LIMIT:
                raise SafetyError("Daily trade limit already exhausted")

            self.db.execute("""
                UPDATE executor_state
                SET state = ?,
                    quantity = ?,
                    trades_today = trades_today + 1,
                    updated_at = ?
                WHERE id = 1
            """, (
                OPEN,
                filled_qty,
                self._timestamp(),
            ))

            self._journal(
                "ENTRY_CONFIRMED",
                f"qty={filled_qty} conId={con_id}"
            )

    def begin_exit(self, broker: BrokerSnapshot):
        """
        OPEN -> EXITING.

        Does NOT submit an exit order.
        """

        self._validate_snapshot(broker)

        with self.db:
            current = self.status()

            if current.state != OPEN:
                raise SafetyError("Executor is not OPEN")

            if current.quantity < 1 or current.con_id is None:
                raise SafetyError("Local OPEN state is contradictory")

            if broker.position_qty != current.quantity:
                raise SafetyError(
                    "Broker quantity disagrees with local OPEN quantity"
                )

            if broker.con_id != current.con_id:
                raise SafetyError(
                    "Broker contract disagrees with local OPEN contract"
                )

            if broker.open_order_count != 0:
                raise SafetyError(
                    "Cannot begin exit with unresolved broker orders"
                )

            self.db.execute("""
                UPDATE executor_state
                SET state = ?,
                    updated_at = ?
                WHERE id = 1
            """, (
                EXITING,
                self._timestamp(),
            ))

            self._journal(
                "EXIT_STARTED",
                f"qty={current.quantity} conId={current.con_id}"
            )

    def confirm_flat(self, broker: BrokerSnapshot):
        """
        EXITING -> FLAT only when broker confirms:
        - zero position
        - zero working orders
        """

        self._validate_snapshot(broker)

        with self.db:
            current = self.status()

            if current.state != EXITING:
                raise SafetyError("Executor is not EXITING")

            if broker.position_qty != 0:
                raise SafetyError(
                    "Cannot declare FLAT while broker owns contracts"
                )

            if broker.con_id is not None:
                raise SafetyError(
                    "Broker flat snapshot contains unexpected conId"
                )

            if broker.open_order_count != 0:
                raise SafetyError(
                    "Cannot declare FLAT with broker orders remaining"
                )

            self.db.execute("""
                UPDATE executor_state
                SET state = ?,
                    direction = NULL,
                    quantity = 0,
                    con_id = NULL,
                    updated_at = ?
                WHERE id = 1
            """, (
                FLAT,
                self._timestamp(),
            ))

            self._journal(
                "FLAT_CONFIRMED",
                "Broker confirms zero position and zero open orders"
            )

    def close(self):
        self.db.close()
