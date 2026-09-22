"""
MORTIFICATIO durable strategy state.

Persists strategy facts that must survive a process/computer restart.

NO IBKR connection.
NO order submission.
Python 3.9 compatible.
"""

import sqlite3
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


CALL = "CALL"
PUT = "PUT"

INITIAL_STOP_POINTS = 3.25
PROFIT_PROTECTION_ARM_POINTS = 4.00
PROFIT_PROTECTION_FLOOR_POINTS = 1.25
LET_IT_RIDE_ARM_POINTS = 5.00
TRAIL_REVERSAL_POINTS = 3.00

NY = ZoneInfo("America/New_York")


class StrategyStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class StrategySnapshot:
    active: bool
    direction: Optional[str]
    con_id: Optional[int]
    entry_spx: Optional[float]
    profit_protection_armed: bool
    let_it_ride_armed: bool
    best_spx: Optional[float]
    exit_required: bool
    exit_reason: Optional[str]


@dataclass(frozen=True)
class TickDecision:
    action: str
    reason: Optional[str]
    favorable_points: float
    best_favorable_points: float
    reversal_points: float


class DurableStrategyState:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path))
        self.conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._create_schema()
        self._initialize()

    def close(self):
        self.conn.close()

    def _now(self):
        return datetime.now(NY).isoformat()

    def _create_schema(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mortificatio_strategy_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                active INTEGER NOT NULL,
                direction TEXT,
                con_id INTEGER,
                entry_spx REAL,
                profit_protection_armed INTEGER NOT NULL DEFAULT 0,
                let_it_ride_armed INTEGER NOT NULL,
                best_spx REAL,
                exit_required INTEGER NOT NULL,
                exit_reason TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

        fields = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(mortificatio_strategy_state)")}
        if "profit_protection_armed" not in fields:
            self.conn.execute(
                "ALTER TABLE mortificatio_strategy_state ADD COLUMN profit_protection_armed INTEGER NOT NULL DEFAULT 0")

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mortificatio_strategy_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    def _initialize(self):
        row = self.conn.execute(
            """
            SELECT singleton
            FROM mortificatio_strategy_state
            WHERE singleton = 1
            """
        ).fetchone()

        if row is not None:
            return

        self.conn.execute(
            """
            INSERT INTO mortificatio_strategy_state (
                singleton,
                active,
                direction,
                con_id,
                entry_spx,
                profit_protection_armed,
                let_it_ride_armed,
                best_spx,
                exit_required,
                exit_reason,
                updated_at
            )
            VALUES (1, 0, NULL, NULL, NULL, 0, 0, NULL, 0, NULL, ?)
            """,
            (self._now(),),
        )

    def _journal(self, event, detail):
        self.conn.execute(
            """
            INSERT INTO mortificatio_strategy_journal (
                event,
                detail,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (event, detail, self._now()),
        )

    def status(self):
        row = self.conn.execute(
            """
            SELECT
                active,
                direction,
                con_id,
                entry_spx,
                profit_protection_armed,
                let_it_ride_armed,
                best_spx,
                exit_required,
                exit_reason
            FROM mortificatio_strategy_state
            WHERE singleton = 1
            """
        ).fetchone()

        if row is None:
            raise StrategyStateError("Strategy state missing")

        if (row[0] not in (0, 1) or row[4] not in (0, 1)
                or row[5] not in (0, 1) or row[7] not in (0, 1)):
            raise StrategyStateError("Strategy flags corrupted")

        snapshot = StrategySnapshot(
            active=bool(row[0]),
            direction=row[1],
            con_id=row[2],
            entry_spx=row[3],
            profit_protection_armed=bool(row[4]),
            let_it_ride_armed=bool(row[5]),
            best_spx=row[6],
            exit_required=bool(row[7]),
            exit_reason=row[8],
        )

        if not snapshot.active:
            if (
                snapshot.direction is not None
                or snapshot.con_id is not None
                or snapshot.entry_spx is not None
                or snapshot.profit_protection_armed
                or snapshot.let_it_ride_armed
                or snapshot.best_spx is not None
                or snapshot.exit_required
                or snapshot.exit_reason is not None
            ):
                raise StrategyStateError(
                    "Inactive strategy contains active trade data"
                )
            return snapshot

        if snapshot.direction not in {CALL, PUT}:
            raise StrategyStateError("Active strategy missing direction")

        if (
            not isinstance(snapshot.con_id, int)
            or snapshot.con_id <= 0
        ):
            raise StrategyStateError("Active strategy missing conId")

        if (
            snapshot.entry_spx is None
            or not math.isfinite(snapshot.entry_spx)
            or snapshot.entry_spx <= 0
        ):
            raise StrategyStateError("Active strategy missing entry SPX")

        if (
            snapshot.best_spx is None
            or not math.isfinite(snapshot.best_spx)
            or snapshot.best_spx <= 0
        ):
            raise StrategyStateError("Active strategy missing best SPX")

        if snapshot.direction == CALL:
            if snapshot.best_spx < snapshot.entry_spx:
                raise StrategyStateError(
                    "CALL best SPX cannot be below entry"
                )
        else:
            if snapshot.best_spx > snapshot.entry_spx:
                raise StrategyStateError(
                    "PUT best SPX cannot be above entry"
                )

        best_favorable = self._favorable_points(
            snapshot.direction, snapshot.entry_spx, snapshot.best_spx)
        if snapshot.profit_protection_armed != (best_favorable >= PROFIT_PROTECTION_ARM_POINTS):
            raise StrategyStateError("Profit protection disagrees with persisted high-water")
        if snapshot.let_it_ride_armed and best_favorable < LET_IT_RIDE_ARM_POINTS:
            raise StrategyStateError("Let It Ride armed without persisted +5")
        if not snapshot.let_it_ride_armed and best_favorable >= LET_IT_RIDE_ARM_POINTS:
            raise StrategyStateError("Persisted +5 without Let It Ride state")

        if snapshot.exit_required and snapshot.exit_reason is None:
            raise StrategyStateError(
                "Exit required without durable exit reason"
            )

        if (
            not snapshot.exit_required
            and snapshot.exit_reason is not None
        ):
            raise StrategyStateError(
                "Exit reason exists without exit requirement"
            )

        return snapshot

    @staticmethod
    def _favorable_points(direction, entry_spx, current_spx):
        if direction == CALL:
            return current_spx - entry_spx
        if direction == PUT:
            return entry_spx - current_spx
        raise StrategyStateError("Invalid direction")

    @staticmethod
    def _better_price(direction, current_spx, best_spx):
        if direction == CALL:
            return max(current_spx, best_spx)
        if direction == PUT:
            return min(current_spx, best_spx)
        raise StrategyStateError("Invalid direction")

    def activate(self, direction, con_id, entry_spx):
        if direction not in {CALL, PUT}:
            raise StrategyStateError("Invalid direction")

        if not isinstance(con_id, int) or con_id <= 0:
            raise StrategyStateError("Invalid conId")

        if entry_spx is None or not math.isfinite(entry_spx) or entry_spx <= 0:
            raise StrategyStateError("Invalid entry SPX")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.status()
            if current.active:
                raise StrategyStateError(
                    "Cannot activate over existing strategy"
                )

            self.conn.execute(
                """
                UPDATE mortificatio_strategy_state
                SET
                    active = 1,
                    direction = ?,
                    con_id = ?,
                    entry_spx = ?,
                    profit_protection_armed = 0,
                    let_it_ride_armed = 0,
                    best_spx = ?,
                    exit_required = 0,
                    exit_reason = NULL,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (
                    direction,
                    con_id,
                    float(entry_spx),
                    float(entry_spx),
                    self._now(),
                ),
            )

            self._journal(
                "STRATEGY_ACTIVATED",
                "direction={} con_id={} entry_spx={}".format(
                    direction,
                    con_id,
                    entry_spx,
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        return self.status()

    def process_spx(self, current_spx):
        if current_spx is None or not math.isfinite(current_spx) or current_spx <= 0:
            raise StrategyStateError("Invalid SPX price")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.status()

            if not current.active:
                raise StrategyStateError(
                    "Cannot process SPX without active strategy"
                )

            if current.exit_required:
                favorable = self._favorable_points(
                    current.direction,
                    current.entry_spx,
                    current_spx,
                )
                best_favorable = self._favorable_points(
                    current.direction,
                    current.entry_spx,
                    current.best_spx,
                )
                reversal = max(
                    0.0,
                    best_favorable - favorable,
                )

                self.conn.execute("COMMIT")
                return TickDecision(
                    action="EXIT",
                    reason=current.exit_reason,
                    favorable_points=favorable,
                    best_favorable_points=best_favorable,
                    reversal_points=reversal,
                )

            new_best = self._better_price(
                current.direction,
                float(current_spx),
                current.best_spx,
            )

            favorable = self._favorable_points(
                current.direction,
                current.entry_spx,
                float(current_spx),
            )

            best_favorable = self._favorable_points(
                current.direction,
                current.entry_spx,
                new_best,
            )

            profit_armed = (
                current.profit_protection_armed
                or best_favorable >= PROFIT_PROTECTION_ARM_POINTS
            )
            armed = (
                current.let_it_ride_armed
                or best_favorable >= LET_IT_RIDE_ARM_POINTS
            )

            reversal = max(
                0.0,
                best_favorable - favorable,
            )

            exit_required = False
            exit_reason = None

            # Before +5, +4 permanently establishes a fixed +1.25 floor.
            # +5 takes precedence permanently.
            if not armed:
                if profit_armed and favorable <= PROFIT_PROTECTION_FLOOR_POINTS:
                    exit_required = True
                    exit_reason = "PROFIT_PROTECTION_FLOOR"
                elif not profit_armed and favorable <= -INITIAL_STOP_POINTS:
                    exit_required = True
                    exit_reason = "INITIAL_STOP"

            # Once +5 has ever been reached, Let It Ride is permanent
            # for this trade and a 3-point reversal from the persisted
            # best favorable excursion requires EXIT ALL.
            else:
                if reversal >= TRAIL_REVERSAL_POINTS:
                    exit_required = True
                    exit_reason = "LET_IT_RIDE_REVERSAL"

            changed = (
                new_best != current.best_spx
                or profit_armed != current.profit_protection_armed
                or armed != current.let_it_ride_armed
                or exit_required
            )

            if changed:
                self.conn.execute(
                    """
                    UPDATE mortificatio_strategy_state
                    SET
                        profit_protection_armed = ?,
                        let_it_ride_armed = ?,
                        best_spx = ?,
                        exit_required = ?,
                        exit_reason = ?,
                        updated_at = ?
                    WHERE singleton = 1
                    """,
                    (
                        1 if profit_armed else 0,
                        1 if armed else 0,
                        float(new_best),
                        1 if exit_required else 0,
                        exit_reason,
                        self._now(),
                    ),
                )

                if profit_armed and not current.profit_protection_armed:
                    self._journal(
                        "PROFIT_PROTECTION_ARMED",
                        "fixed_favorable_floor_points=1.25",
                    )

                if (
                    armed
                    and not current.let_it_ride_armed
                ):
                    self._journal(
                        "LET_IT_RIDE_ARMED",
                        "best_favorable_points={:.4f}".format(
                            best_favorable
                        ),
                    )

                if new_best != current.best_spx:
                    self._journal(
                        "HIGH_WATER_UPDATED",
                        "best_spx={} best_favorable_points={:.4f}".format(
                            new_best,
                            best_favorable,
                        ),
                    )

                if exit_required:
                    self._journal(
                        "EXIT_REQUIRED",
                        (
                            "reason={} favorable_points={:.4f} "
                            "best_favorable_points={:.4f} "
                            "reversal_points={:.4f}"
                        ).format(
                            exit_reason,
                            favorable,
                            best_favorable,
                            reversal,
                        ),
                    )

            self.conn.execute("COMMIT")

            return TickDecision(
                action="EXIT" if exit_required else "HOLD",
                reason=exit_reason,
                favorable_points=favorable,
                best_favorable_points=best_favorable,
                reversal_points=reversal,
            )

        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    def clear_after_broker_flat(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.status()

            if not current.active:
                raise StrategyStateError(
                    "No active strategy to clear"
                )

            if not current.exit_required:
                raise StrategyStateError(
                    "Strategy cannot clear before durable exit intent"
                )

            self.conn.execute(
                """
                UPDATE mortificatio_strategy_state
                SET
                    active = 0,
                    direction = NULL,
                    con_id = NULL,
                    entry_spx = NULL,
                    profit_protection_armed = 0,
                    let_it_ride_armed = 0,
                    best_spx = NULL,
                    exit_required = 0,
                    exit_reason = NULL,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (self._now(),),
            )

            self._journal(
                "STRATEGY_CLEARED",
                "Cleared only after caller proved broker FLAT",
            )

            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        return self.status()
