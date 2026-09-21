"""
MORTIFICATIO persistent safety state.

Simulation / state-management layer only.
NO IBKR connection.
NO order submission.

Key safety property added here:
The quantity authorized BEFORE entry is persisted separately from
the quantity actually owned.

Therefore crash recovery can prove that a broker position does not
exceed what MORTIFICATIO originally authorized.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


MAX_CONTRACTS = 25
DAILY_TRADE_LIMIT = 2

FLAT = "FLAT"
ENTERING = "ENTERING"
OPEN = "OPEN"
EXITING = "EXITING"

CALL = "CALL"
PUT = "PUT"

NY = ZoneInfo("America/New_York")


class MortificatioStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerSnapshot:
    complete: bool
    position_qty: int
    con_id: Optional[int]
    open_order_count: int


@dataclass(frozen=True)
class StateSnapshot:
    state: str
    direction: Optional[str]
    planned_quantity: int
    quantity: int
    con_id: Optional[int]
    trades_today: int
    trading_day: str


class MortificatioState:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path))

        self.conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,
        )

        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")

        self._create_schema()
        self._initialize()

    def close(self):
        self.conn.close()

    def _today(self):
        return datetime.now(NY).date().isoformat()

    def _now(self):
        return datetime.now(NY).isoformat()

    def _create_schema(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mortificatio_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),

                state TEXT NOT NULL,
                direction TEXT,

                planned_quantity INTEGER NOT NULL,
                quantity INTEGER NOT NULL,

                con_id INTEGER,

                trades_today INTEGER NOT NULL,
                trading_day TEXT NOT NULL,

                updated_at TEXT NOT NULL
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mortificatio_event_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mortificatio_account_binding (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                account_id TEXT NOT NULL
            )
            """
        )

    def bind_selected_account(self, account_id):
        if not isinstance(account_id, str) or not account_id.strip():
            raise MortificatioStateError("Selected account missing")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT account_id FROM mortificatio_account_binding WHERE singleton = 1"
            ).fetchone()
            if row is None:
                current = self.status()
                if current.state != FLAT or current.trades_today != 0:
                    raise MortificatioStateError("Cannot establish account during lifecycle")
                self.conn.execute(
                    "INSERT INTO mortificatio_account_binding VALUES (1, ?)",
                    (account_id,),
                )
            elif row[0] != account_id:
                raise MortificatioStateError("Selected account differs from persisted binding")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def selected_account(self):
        row = self.conn.execute(
            "SELECT account_id FROM mortificatio_account_binding WHERE singleton = 1"
        ).fetchone()
        if row is None or not row[0]:
            raise MortificatioStateError("Persisted account binding missing")
        return row[0]

    def reconcile_verified_broker(self, broker, selected_account, now_monotonic):
        """Read-only reconciliation; uncertainty only blocks new entry."""
        from simulation_evidence import validate_broker_snapshot

        validate_broker_snapshot(broker, selected_account, now_monotonic)
        if self.selected_account() != selected_account:
            raise MortificatioStateError("Snapshot account differs from persisted binding")
        local = self.status()
        if local.state == FLAT:
            if broker.position_qty == 0 and broker.open_order_count == 0:
                return "ENTRY_ELIGIBLE"
            return "BLOCK_ENTRY"
        if local.state == OPEN:
            if (broker.position_qty == local.quantity
                    and broker.con_id == local.con_id
                    and broker.open_order_count == 0):
                return "MANAGE_EXISTING"
            return "BLOCK_ENTRY"
        return "BLOCK_ENTRY"

    def _initialize(self):
        row = self.conn.execute(
            """
            SELECT singleton
            FROM mortificatio_state
            WHERE singleton = 1
            """
        ).fetchone()

        if row is not None:
            return

        self.conn.execute(
            """
            INSERT INTO mortificatio_state (
                singleton,
                state,
                direction,
                planned_quantity,
                quantity,
                con_id,
                trades_today,
                trading_day,
                updated_at
            )
            VALUES (1, ?, NULL, 0, 0, NULL, 0, ?, ?)
            """,
            (
                FLAT,
                self._today(),
                self._now(),
            ),
        )

        self._journal(
            "INITIALIZED",
            "Persistent MORTIFICATIO state initialized FLAT",
        )

    def _journal(self, event, detail):
        self.conn.execute(
            """
            INSERT INTO mortificatio_event_journal (
                event,
                detail,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                event,
                detail,
                self._now(),
            ),
        )

    def _validate_snapshot(self, broker):
        if not isinstance(broker, BrokerSnapshot):
            raise MortificatioStateError(
                "Invalid broker snapshot object"
            )

        if broker.complete is not True:
            raise MortificatioStateError(
                "Broker snapshot incomplete"
            )

        if (
            not isinstance(broker.position_qty, int)
            or isinstance(broker.position_qty, bool)
            or broker.position_qty < 0
            or broker.position_qty > MAX_CONTRACTS
        ):
            raise MortificatioStateError(
                "Broker position quantity outside safety bounds"
            )

        if (
            not isinstance(broker.open_order_count, int)
            or isinstance(broker.open_order_count, bool)
            or broker.open_order_count < 0
        ):
            raise MortificatioStateError(
                "Invalid broker open-order count"
            )

        if broker.position_qty == 0:
            if broker.con_id is not None:
                raise MortificatioStateError(
                    "Zero broker quantity must not carry conId"
                )

        else:
            if (
                not isinstance(broker.con_id, int)
                or isinstance(broker.con_id, bool)
                or broker.con_id <= 0
            ):
                raise MortificatioStateError(
                    "Broker position missing valid conId"
                )

    def status(self):
        row = self.conn.execute(
            """
            SELECT
                state,
                direction,
                planned_quantity,
                quantity,
                con_id,
                trades_today,
                trading_day
            FROM mortificatio_state
            WHERE singleton = 1
            """
        ).fetchone()

        if row is None:
            raise MortificatioStateError(
                "Persistent state missing"
            )

        snapshot = StateSnapshot(*row)

        if snapshot.state not in {
            FLAT,
            ENTERING,
            OPEN,
            EXITING,
        }:
            raise MortificatioStateError(
                "Unknown persistent lifecycle state"
            )

        if (
            snapshot.planned_quantity < 0
            or snapshot.planned_quantity > MAX_CONTRACTS
        ):
            raise MortificatioStateError(
                "Persistent planned quantity corrupted"
            )

        if (
            snapshot.quantity < 0
            or snapshot.quantity > MAX_CONTRACTS
        ):
            raise MortificatioStateError(
                "Persistent owned quantity corrupted"
            )

        if (
            not isinstance(snapshot.trades_today, int)
            or snapshot.trades_today < 0
            or snapshot.trades_today > DAILY_TRADE_LIMIT
        ):
            raise MortificatioStateError(
                "Persistent trade count corrupted"
            )

        try:
            if date.fromisoformat(snapshot.trading_day).isoformat() != snapshot.trading_day:
                raise ValueError
        except (TypeError, ValueError):
            raise MortificatioStateError("Persistent trading date corrupted")

        if snapshot.state == FLAT:
            if (
                snapshot.direction is not None
                or snapshot.planned_quantity != 0
                or snapshot.quantity != 0
                or snapshot.con_id is not None
            ):
                raise MortificatioStateError(
                    "Impossible FLAT state"
                )

        if snapshot.state == ENTERING:
            if snapshot.direction not in {CALL, PUT}:
                raise MortificatioStateError(
                    "ENTERING missing direction"
                )

            if not (
                1 <= snapshot.planned_quantity <= MAX_CONTRACTS
            ):
                raise MortificatioStateError(
                    "ENTERING missing authorized quantity"
                )

            if snapshot.quantity != 0:
                raise MortificatioStateError(
                    "ENTERING local owned quantity must remain zero "
                    "until broker reconciliation"
                )

            if (
                snapshot.con_id is None
                or snapshot.con_id <= 0
            ):
                raise MortificatioStateError(
                    "ENTERING missing conId"
                )

        if snapshot.state in {OPEN, EXITING}:
            if snapshot.direction not in {CALL, PUT}:
                raise MortificatioStateError(
                    "Active position missing direction"
                )

            if not (
                1 <= snapshot.planned_quantity <= MAX_CONTRACTS
            ):
                raise MortificatioStateError(
                    "Active position missing authorized quantity"
                )

            if not (
                1 <= snapshot.quantity
                <= snapshot.planned_quantity
            ):
                raise MortificatioStateError(
                    "Owned quantity exceeds authorized quantity"
                )

            if (
                snapshot.con_id is None
                or snapshot.con_id <= 0
            ):
                raise MortificatioStateError(
                    "Active position missing conId"
                )

        return snapshot

    def _roll_day_if_safe(self):
        today = self._today()

        self.conn.execute("BEGIN IMMEDIATE")

        try:
            current = self.status()
            if today < current.trading_day:
                raise MortificatioStateError("New York clock moved backward")
            if today == current.trading_day:
                self.conn.execute("COMMIT")
                return
            if current.state != FLAT or current.quantity != 0:
                raise MortificatioStateError(
                    "Trading day changed during active lifecycle"
                )
            self.conn.execute(
                """
                UPDATE mortificatio_state
                SET
                    trades_today = 0,
                    trading_day = ?,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (
                    today,
                    self._now(),
                ),
            )

            self._journal(
                "DAY_ROLLOVER",
                f"Trading day rolled to {today}",
            )

            self.conn.execute("COMMIT")

        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def request_entry(
        self,
        direction,
        planned_qty,
        planned_con_id,
        broker,
    ):
        self._roll_day_if_safe()
        self._validate_snapshot(broker)

        if direction not in {CALL, PUT}:
            raise MortificatioStateError(
                "Invalid direction"
            )

        if (
            not isinstance(planned_qty, int)
            or planned_qty < 1
            or planned_qty > MAX_CONTRACTS
        ):
            raise MortificatioStateError(
                "Invalid planned quantity"
            )

        if (
            not isinstance(planned_con_id, int)
            or planned_con_id <= 0
        ):
            raise MortificatioStateError(
                "Invalid planned conId"
            )

        current = self.status()

        if current.state != FLAT:
            raise MortificatioStateError(
                "Entry requires FLAT state"
            )

        if current.trades_today >= DAILY_TRADE_LIMIT:
            raise MortificatioStateError(
                "Daily two-trade limit reached"
            )

        if (
            broker.position_qty != 0
            or broker.con_id is not None
            or broker.open_order_count != 0
        ):
            raise MortificatioStateError(
                "Broker is not provably flat"
            )

        self.conn.execute("BEGIN IMMEDIATE")

        try:
            fresh = self.status()

            if fresh.state != FLAT:
                raise MortificatioStateError(
                    "Concurrent lifecycle change detected"
                )

            if fresh.trades_today >= DAILY_TRADE_LIMIT:
                raise MortificatioStateError(
                    "Daily two-trade limit reached"
                )

            self.conn.execute(
                """
                UPDATE mortificatio_state
                SET
                    state = ?,
                    direction = ?,
                    planned_quantity = ?,
                    quantity = 0,
                    con_id = ?,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (
                    ENTERING,
                    direction,
                    planned_qty,
                    planned_con_id,
                    self._now(),
                ),
            )

            self._journal(
                "ENTRY_RESERVED",
                (
                    f"direction={direction} "
                    f"planned_quantity={planned_qty} "
                    f"con_id={planned_con_id}"
                ),
            )

            self.conn.execute("COMMIT")

        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        return self.status()

    def recover_entry(self, broker):
        self._validate_snapshot(broker)

        current = self.status()

        if current.state != ENTERING:
            raise MortificatioStateError(
                "Entry recovery requires ENTERING"
            )

        if current.trading_day != self._today():
            raise MortificatioStateError(
                "Entry fill date cannot be proven from broker snapshot"
            )

        if broker.open_order_count != 0:
            raise MortificatioStateError(
                "Cannot finalize while broker order unresolved"
            )

        if broker.position_qty == 0:
            raise MortificatioStateError(
                "No broker fill exists to recover"
            )

        if broker.con_id != current.con_id:
            raise MortificatioStateError(
                "Broker contract differs from reserved contract"
            )

        if broker.position_qty > current.planned_quantity:
            raise MortificatioStateError(
                "Broker quantity exceeds authorized entry quantity"
            )

        self.conn.execute("BEGIN IMMEDIATE")

        try:
            fresh = self.status()

            if fresh.state != ENTERING:
                raise MortificatioStateError(
                    "Concurrent recovery detected"
                )

            if fresh.trading_day != self._today():
                raise MortificatioStateError("Entry fill date changed")

            if fresh.con_id != broker.con_id:
                raise MortificatioStateError(
                    "Persistent contract changed during recovery"
                )

            if broker.position_qty > fresh.planned_quantity:
                raise MortificatioStateError(
                    "Broker quantity exceeds persistent authorization"
                )

            if fresh.trades_today >= DAILY_TRADE_LIMIT:
                raise MortificatioStateError(
                    "Trade count cannot be incremented safely"
                )

            self.conn.execute(
                """
                UPDATE mortificatio_state
                SET
                    state = ?,
                    quantity = ?,
                    trades_today = trades_today + 1,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (
                    OPEN,
                    broker.position_qty,
                    self._now(),
                ),
            )

            self._journal(
                "ENTRY_RECOVERED",
                (
                    f"quantity={broker.position_qty} "
                    f"authorized={fresh.planned_quantity} "
                    f"con_id={broker.con_id}"
                ),
            )

            self.conn.execute("COMMIT")

        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        return self.status()

    def begin_exit(self, broker):
        self._validate_snapshot(broker)

        current = self.status()

        if current.state != OPEN:
            raise MortificatioStateError(
                "Exit requires OPEN state"
            )

        if broker.open_order_count != 0:
            raise MortificatioStateError(
                "Cannot begin exit with unresolved broker order"
            )

        if (
            broker.position_qty != current.quantity
            or broker.con_id != current.con_id
        ):
            raise MortificatioStateError(
                "Broker position does not match persistent position"
            )

        self.conn.execute("BEGIN IMMEDIATE")

        try:
            fresh = self.status()

            if fresh.state != OPEN:
                raise MortificatioStateError(
                    "Concurrent exit transition detected"
                )

            self.conn.execute(
                """
                UPDATE mortificatio_state
                SET
                    state = ?,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (
                    EXITING,
                    self._now(),
                ),
            )

            self._journal(
                "EXIT_IRREVERSIBLE",
                (
                    f"quantity={fresh.quantity} "
                    f"con_id={fresh.con_id}"
                ),
            )

            self.conn.execute("COMMIT")

        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        return self.status()

    def confirm_flat(self, broker):
        self._validate_snapshot(broker)

        current = self.status()

        if current.state != EXITING:
            raise MortificatioStateError(
                "Flat confirmation requires EXITING"
            )

        if (
            broker.position_qty != 0
            or broker.con_id is not None
            or broker.open_order_count != 0
        ):
            raise MortificatioStateError(
                "Broker has not proven flat"
            )

        self.conn.execute("BEGIN IMMEDIATE")

        try:
            fresh = self.status()

            if fresh.state != EXITING:
                raise MortificatioStateError(
                    "Concurrent flat transition detected"
                )

            self.conn.execute(
                """
                UPDATE mortificatio_state
                SET
                    state = ?,
                    direction = NULL,
                    planned_quantity = 0,
                    quantity = 0,
                    con_id = NULL,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (
                    FLAT,
                    self._now(),
                ),
            )

            self._journal(
                "BROKER_CONFIRMED_FLAT",
                "IBKR truth reconciled to zero position and zero orders",
            )

            self.conn.execute("COMMIT")

        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        return self.status()
