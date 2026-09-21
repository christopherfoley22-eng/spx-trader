import os
import sqlite3
import tempfile

from executor_engine import (
    Executor,
    Direction,
    EngineState,
    StrategyState,
    Position,
    OptionCandidate,
)


class PersistentExecutor(Executor):
    """
    Dry-run persistence wrapper around Executor.

    SQLite records what Executor believed before a crash.
    It does NOT replace IBKR reconciliation.
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._initialize_database()

        super().__init__()

        self._restore()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _initialize_database(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS executor_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),

                    engine_state TEXT NOT NULL,
                    trades_today INTEGER NOT NULL,

                    direction TEXT,
                    con_id INTEGER,
                    strike REAL,
                    quantity INTEGER,
                    entry_spx REAL,

                    strategy_state TEXT,
                    max_favorable REAL
                )
            """)

            row = conn.execute(
                "SELECT id FROM executor_state WHERE id = 1"
            ).fetchone()

            if row is None:
                conn.execute("""
                    INSERT INTO executor_state (
                        id,
                        engine_state,
                        trades_today
                    )
                    VALUES (1, 'FLAT', 0)
                """)

            conn.commit()

    def _restore(self):
        with self._connect() as conn:
            row = conn.execute("""
                SELECT
                    engine_state,
                    trades_today,
                    direction,
                    con_id,
                    strike,
                    quantity,
                    entry_spx,
                    strategy_state,
                    max_favorable
                FROM executor_state
                WHERE id = 1
            """).fetchone()

        if row is None:
            raise RuntimeError("PERSISTENT STATE MISSING")

        (
            engine_state,
            trades_today,
            direction,
            con_id,
            strike,
            quantity,
            entry_spx,
            strategy_state,
            max_favorable,
        ) = row

        self.state = EngineState[engine_state]
        self.trades_today = trades_today

        if self.state == EngineState.FLAT:
            self.position = None
            return

        if self.state == EngineState.ENTERING:
            # ENTERING is deliberately restored without inventing a fill.
            # Broker reconciliation must determine what actually happened.
            self.position = None
            return

        if direction is None:
            raise RuntimeError(
                "ACTIVE STATE MISSING DIRECTION"
            )

        if con_id is None or quantity is None:
            raise RuntimeError(
                "ACTIVE STATE MISSING POSITION DATA"
            )

        self.position = Position(
            direction=Direction[direction],
            con_id=con_id,
            strike=strike,
            quantity=quantity,
            entry_spx=entry_spx,
            strategy_state=StrategyState[strategy_state],
            max_favorable=max_favorable,
        )

    def persist(self):
        p = self.position

        with self._connect() as conn:
            conn.execute("""
                UPDATE executor_state
                SET
                    engine_state = ?,
                    trades_today = ?,
                    direction = ?,
                    con_id = ?,
                    strike = ?,
                    quantity = ?,
                    entry_spx = ?,
                    strategy_state = ?,
                    max_favorable = ?
                WHERE id = 1
            """, (
                self.state.name,
                self.trades_today,

                p.direction.name if p else None,
                p.con_id if p else None,
                p.strike if p else None,
                p.quantity if p else None,
                p.entry_spx if p else None,

                p.strategy_state.name if p else None,
                p.max_favorable if p else None,
            ))

            conn.commit()

    def request_entry(self, *args, **kwargs):
        result = super().request_entry(*args, **kwargs)
        self.persist()
        return result

    def simulate_entry_fill(self, *args, **kwargs):
        result = super().simulate_entry_fill(*args, **kwargs)
        self.persist()
        return result

    def update_spx(self, *args, **kwargs):
        result = super().update_spx(*args, **kwargs)
        self.persist()
        return result

    def simulate_exit_fill(self, *args, **kwargs):
        result = super().simulate_exit_fill(*args, **kwargs)
        self.persist()
        return result


def candidates():
    return [
        OptionCandidate(1001, 7695, 11.00),
        OptionCandidate(1002, 7700, 10.00),
        OptionCandidate(1003, 7705, 9.00),
    ]


fd, db_path = tempfile.mkstemp(
    prefix="executor_crash_test_",
    suffix=".db"
)
os.close(fd)


try:

    print()
    print("========================================")
    print("CRASH / RESTART ATTACK TESTS")
    print("========================================")


    # ========================================================
    # 1. ENTERING SURVIVES RESTART
    # ========================================================

    print("1. ENTERING SURVIVES RESTART")

    e = PersistentExecutor(db_path)

    r = e.request_entry(
        Direction.CALL,
        7700,
        25000,
        candidates(),
        True,
        0,
        0,
    )

    assert r["event"] == "DRY_RUN_ENTRY_REQUEST"
    assert e.state == EngineState.ENTERING

    # Simulated process death:
    del e

    e = PersistentExecutor(db_path)

    assert e.state == EngineState.ENTERING
    assert e.position is None

    print("PASS")


    # For this simulation only, reconciliation determines
    # that the intended entry did NOT actually fill.
    e.state = EngineState.FLAT
    e.position = None
    e.persist()


    # ========================================================
    # 2. OPEN POSITION SURVIVES RESTART
    # ========================================================

    print("2. OPEN POSITION SURVIVES RESTART")

    e = PersistentExecutor(db_path)

    e.request_entry(
        Direction.CALL,
        7700,
        25000,
        candidates(),
        True,
        0,
        0,
    )

    e.simulate_entry_fill(
        Direction.CALL,
        con_id=1002,
        strike=7700,
        quantity=25,
        entry_spx=7700,
    )

    assert e.state == EngineState.OPEN
    assert e.trades_today == 1

    del e

    e = PersistentExecutor(db_path)

    assert e.state == EngineState.OPEN
    assert e.position is not None
    assert e.position.direction == Direction.CALL
    assert e.position.con_id == 1002
    assert e.position.quantity == 25
    assert e.position.entry_spx == 7700
    assert e.trades_today == 1

    print("PASS")


    # ========================================================
    # 3. STRATEGY STATE SURVIVES RESTART
    # ========================================================

    print("3. LET-IT-RIDE STATE SURVIVES RESTART")

    e.update_spx(7705.00)
    e.update_spx(7713.50)

    assert (
        e.position.strategy_state
        == StrategyState.LET_IT_RIDE
    )

    assert e.position.max_favorable == 13.50

    del e

    e = PersistentExecutor(db_path)

    assert e.state == EngineState.OPEN
    assert (
        e.position.strategy_state
        == StrategyState.LET_IT_RIDE
    )
    assert e.position.max_favorable == 13.50

    print("PASS")


    # ========================================================
    # 4. TRAILING PROTECTION STILL WORKS AFTER RESTART
    # ========================================================

    print("4. TRAIL STILL WORKS AFTER RESTART")

    # Peak = +13.50
    # 2.99 reversal must hold.
    r = e.update_spx(7710.51)

    assert r is None
    assert e.state == EngineState.OPEN

    # Exactly 3.00 must exit.
    r = e.update_spx(7710.50)

    assert r is not None
    assert r["reason"] == "LET_IT_RIDE_TRAIL"
    assert e.state == EngineState.EXITING

    print("PASS")


    # ========================================================
    # 5. EXITING SURVIVES RESTART
    # ========================================================

    print("5. EXITING SURVIVES RESTART")

    del e

    e = PersistentExecutor(db_path)

    assert e.state == EngineState.EXITING
    assert e.position is not None
    assert e.position.quantity == 25
    assert e.position.con_id == 1002

    print("PASS")


    # ========================================================
    # 6. RESTART DOES NOT ALLOW NEW ENTRY WHILE EXITING
    # ========================================================

    print("6. ENTRY BLOCKED AFTER EXITING RESTART")

    r = e.request_entry(
        Direction.PUT,
        7700,
        25000,
        candidates(),
        True,
        0,
        0,
    )

    assert r["event"] == "ENTRY_BLOCKED"
    assert r["reason"] == "LOCAL_STATE_NOT_FLAT"

    print("PASS")


    # ========================================================
    # 7. CONFIRMED FLAT SURVIVES RESTART
    # ========================================================

    print("7. CONFIRMED FLAT SURVIVES RESTART")

    e.simulate_exit_fill()

    assert e.state == EngineState.FLAT
    assert e.position is None

    del e

    e = PersistentExecutor(db_path)

    assert e.state == EngineState.FLAT
    assert e.position is None
    assert e.trades_today == 1

    print("PASS")


    # ========================================================
    # 8. SECOND TRADE COUNT SURVIVES RESTART
    # ========================================================

    print("8. DAILY COUNT SURVIVES RESTART")

    e.request_entry(
        Direction.PUT,
        7700,
        25000,
        candidates(),
        True,
        0,
        0,
    )

    e.simulate_entry_fill(
        Direction.PUT,
        con_id=1002,
        strike=7700,
        quantity=25,
        entry_spx=7700,
    )

    assert e.trades_today == 2

    # Stop second trade.
    r = e.update_spx(7703.25)

    assert r["reason"] == "INITIAL_STOP"

    e.simulate_exit_fill()

    del e

    e = PersistentExecutor(db_path)

    assert e.state == EngineState.FLAT
    assert e.trades_today == 2

    print("PASS")


    # ========================================================
    # 9. RESTART CANNOT RESET DAILY LIMIT
    # ========================================================

    print("9. THIRD TRADE BLOCKED AFTER RESTART")

    r = e.request_entry(
        Direction.CALL,
        7700,
        25000,
        candidates(),
        True,
        0,
        0,
    )

    assert r["event"] == "ENTRY_BLOCKED"
    assert r["reason"] == "DAILY_TRADE_LIMIT"

    print("PASS")


    print()
    print("========================================")
    print("ALL CRASH / RESTART TESTS PASS")
    print("========================================")
    print("ENTERING PERSISTENCE:       PASS")
    print("OPEN POSITION PERSISTENCE:  PASS")
    print("STRATEGY STATE PERSISTENCE: PASS")
    print("HIGH-WATER PERSISTENCE:     PASS")
    print("EXITING PERSISTENCE:        PASS")
    print("DAILY COUNT PERSISTENCE:    PASS")
    print("RESTART ENTRY BLOCK:        PASS")
    print()
    print("DRY RUN ONLY")
    print("ZERO IBKR ORDERS")
    print("========================================")
    print()

finally:
    try:
        os.remove(db_path)
    except FileNotFoundError:
        pass
