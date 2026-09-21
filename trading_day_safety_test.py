import os
import sqlite3
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")
DAILY_TRADE_LIMIT = 2


class TradingDayGuard:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS trading_day_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    trading_date TEXT,
                    trades INTEGER NOT NULL DEFAULT 0
                )
            """)

            row = con.execute(
                "SELECT id FROM trading_day_state WHERE id = 1"
            ).fetchone()

            if row is None:
                con.execute("""
                    INSERT INTO trading_day_state
                    (id, trading_date, trades)
                    VALUES (1, NULL, 0)
                """)

    def _ny_date(self, dt):
        if dt.tzinfo is None:
            raise ValueError("NAIVE_DATETIME_BLOCKED")

        return dt.astimezone(NY).date().isoformat()

    def snapshot(self):
        with self._connect() as con:
            row = con.execute("""
                SELECT trading_date, trades
                FROM trading_day_state
                WHERE id = 1
            """).fetchone()

        if row is None:
            raise RuntimeError("MISSING_STATE_ROW")

        return row[0], row[1]

    def reconcile_date(self, now):
        current_date = self._ny_date(now)

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
                SELECT trading_date, trades
                FROM trading_day_state
                WHERE id = 1
            """).fetchone()

            if row is None:
                con.rollback()
                return False, "MISSING_STATE"

            stored_date, trades = row

            if not isinstance(trades, int):
                con.rollback()
                return False, "INVALID_TRADE_COUNT"

            if trades < 0 or trades > DAILY_TRADE_LIMIT:
                con.rollback()
                return False, "INVALID_TRADE_COUNT"

            if stored_date is None:
                con.execute("""
                    UPDATE trading_day_state
                    SET trading_date = ?, trades = 0
                    WHERE id = 1
                """, (current_date,))

                con.commit()
                return True, "DATE_INITIALIZED"

            if current_date < stored_date:
                con.rollback()
                return False, "CLOCK_ROLLBACK_BLOCKED"

            if current_date > stored_date:
                con.execute("""
                    UPDATE trading_day_state
                    SET trading_date = ?, trades = 0
                    WHERE id = 1
                """, (current_date,))

                con.commit()
                return True, "NEW_TRADING_DATE"

            con.commit()
            return True, "SAME_TRADING_DATE"

    def can_start_trade(self, now):
        ok, reason = self.reconcile_date(now)

        if not ok:
            return False, reason

        _, trades = self.snapshot()

        if trades >= DAILY_TRADE_LIMIT:
            return False, "DAILY_LIMIT"

        return True, "TRADE_ALLOWED"

    def record_first_fill(self, now):
        current_date = self._ny_date(now)

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
                SELECT trading_date, trades
                FROM trading_day_state
                WHERE id = 1
            """).fetchone()

            if row is None:
                con.rollback()
                return False, "MISSING_STATE"

            stored_date, trades = row

            if stored_date is None:
                con.rollback()
                return False, "DATE_NOT_INITIALIZED"

            if current_date < stored_date:
                con.rollback()
                return False, "CLOCK_ROLLBACK_BLOCKED"

            if current_date > stored_date:
                # Important:
                # record_first_fill NEVER silently resets the day.
                # The date must first be reconciled through the
                # normal pre-trade gate.
                con.rollback()
                return False, "DATE_NOT_RECONCILED"

            if (
                not isinstance(trades, int)
                or trades < 0
                or trades > DAILY_TRADE_LIMIT
            ):
                con.rollback()
                return False, "INVALID_TRADE_COUNT"

            if trades >= DAILY_TRADE_LIMIT:
                con.rollback()
                return False, "DAILY_LIMIT"

            new_count = trades + 1

            con.execute("""
                UPDATE trading_day_state
                SET trades = ?
                WHERE id = 1
            """, (new_count,))

            con.commit()

            return True, "TRADE_RECORDED"


def dt(year, month, day, hour, minute=0):
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=NY,
    )


def expect(condition, message):
    assert condition, message


fd, path = tempfile.mkstemp(
    prefix="executor_day_",
    suffix=".db",
)

os.close(fd)


print()
print("========================================")
print("TRADING-DAY / DAILY-CAP ATTACK TESTS")
print("========================================")


try:
    # --------------------------------------------------------
    # 1. First startup initializes NY trading date.
    # --------------------------------------------------------

    g = TradingDayGuard(path)

    ok, reason = g.can_start_trade(
        dt(2026, 9, 21, 9, 30)
    )

    expect(ok, reason)

    stored_date, trades = g.snapshot()

    expect(stored_date == "2026-09-21", stored_date)
    expect(trades == 0, trades)

    print("1. NEW YORK TRADING DATE INITIALIZED: PASS")


    # --------------------------------------------------------
    # 2. First actual fill counts trade #1.
    # --------------------------------------------------------

    ok, reason = g.record_first_fill(
        dt(2026, 9, 21, 9, 31)
    )

    expect(ok, reason)

    _, trades = g.snapshot()
    expect(trades == 1, trades)

    print("2. FIRST FILLED TRADE PERSISTED: PASS")


    # --------------------------------------------------------
    # 3. Restart cannot erase trade count.
    # --------------------------------------------------------

    del g

    g = TradingDayGuard(path)

    stored_date, trades = g.snapshot()

    expect(stored_date == "2026-09-21", stored_date)
    expect(trades == 1, trades)

    print("3. PROCESS RESTART PRESERVES DAILY COUNT: PASS")


    # --------------------------------------------------------
    # 4. Second trade is still allowed.
    # --------------------------------------------------------

    ok, reason = g.can_start_trade(
        dt(2026, 9, 21, 12, 0)
    )

    expect(ok, reason)

    print("4. SECOND DAILY TRADE ALLOWED: PASS")


    # --------------------------------------------------------
    # 5. Second first-fill produces count 2.
    # --------------------------------------------------------

    ok, reason = g.record_first_fill(
        dt(2026, 9, 21, 12, 1)
    )

    expect(ok, reason)

    _, trades = g.snapshot()
    expect(trades == 2, trades)

    print("5. SECOND FILLED TRADE PERSISTED: PASS")


    # --------------------------------------------------------
    # 6. Third trade blocked.
    # --------------------------------------------------------

    ok, reason = g.can_start_trade(
        dt(2026, 9, 21, 13, 0)
    )

    expect(not ok, "Third trade was allowed")
    expect(reason == "DAILY_LIMIT", reason)

    print("6. THIRD DAILY TRADE BLOCKED: PASS")


    # --------------------------------------------------------
    # 7. Restart after two trades still blocks trade #3.
    # --------------------------------------------------------

    del g

    g = TradingDayGuard(path)

    ok, reason = g.can_start_trade(
        dt(2026, 9, 21, 14, 0)
    )

    expect(not ok, "Restart bypassed cap")
    expect(reason == "DAILY_LIMIT", reason)

    print("7. RESTART CANNOT BYPASS DAILY CAP: PASS")


    # --------------------------------------------------------
    # 8. Earlier time on SAME date does not reset count.
    # --------------------------------------------------------

    ok, reason = g.can_start_trade(
        dt(2026, 9, 21, 8, 0)
    )

    expect(not ok, "Same-date clock change reset cap")
    expect(reason == "DAILY_LIMIT", reason)

    print("8. SAME-DATE CLOCK CHANGE CANNOT RESET CAP: PASS")


    # --------------------------------------------------------
    # 9. Calendar date moving backward is blocked.
    # --------------------------------------------------------

    ok, reason = g.can_start_trade(
        dt(2026, 9, 20, 15, 0)
    )

    expect(not ok, "Backward date accepted")
    expect(reason == "CLOCK_ROLLBACK_BLOCKED", reason)

    stored_date, trades = g.snapshot()

    expect(stored_date == "2026-09-21", stored_date)
    expect(trades == 2, trades)

    print("9. CALENDAR ROLLBACK BLOCKED: PASS")


    # --------------------------------------------------------
    # 10. A later NY date starts a new allowance.
    # --------------------------------------------------------

    ok, reason = g.can_start_trade(
        dt(2026, 9, 22, 9, 30)
    )

    expect(ok, reason)

    stored_date, trades = g.snapshot()

    expect(stored_date == "2026-09-22", stored_date)
    expect(trades == 0, trades)

    print("10. NEW NY DATE RESETS DAILY COUNT: PASS")


    # --------------------------------------------------------
    # 11. New day's first fill works.
    # --------------------------------------------------------

    ok, reason = g.record_first_fill(
        dt(2026, 9, 22, 9, 31)
    )

    expect(ok, reason)

    _, trades = g.snapshot()
    expect(trades == 1, trades)

    print("11. NEW-DATE FIRST TRADE RECORDED: PASS")


    # --------------------------------------------------------
    # 12. UTC/local representation resolves to NY date.
    # --------------------------------------------------------

    utc = ZoneInfo("UTC")

    same_instant = datetime(
        2026,
        9,
        22,
        16,
        0,
        tzinfo=utc,
    )

    ok, reason = g.can_start_trade(same_instant)

    expect(ok, reason)

    stored_date, trades = g.snapshot()

    expect(stored_date == "2026-09-22", stored_date)
    expect(trades == 1, trades)

    print("12. TIMEZONE CONVERSION USES NEW YORK DATE: PASS")


    # --------------------------------------------------------
    # 13. Naive datetime is rejected.
    # --------------------------------------------------------

    try:
        g.can_start_trade(
            datetime(2026, 9, 22, 12, 0)
        )

        raise AssertionError("Naive datetime accepted")

    except ValueError as exc:
        expect(
            str(exc) == "NAIVE_DATETIME_BLOCKED",
            str(exc),
        )

    print("13. TIMEZONE-LESS CLOCK INPUT BLOCKED: PASS")


    # --------------------------------------------------------
    # 14. Corrupted negative count fails closed.
    # --------------------------------------------------------

    with sqlite3.connect(path) as con:
        con.execute("""
            UPDATE trading_day_state
            SET trades = -1
            WHERE id = 1
        """)

    ok, reason = g.can_start_trade(
        dt(2026, 9, 22, 13, 0)
    )

    expect(not ok, "Negative count accepted")
    expect(reason == "INVALID_TRADE_COUNT", reason)

    print("14. NEGATIVE PERSISTED COUNT -> BLOCKED: PASS")


    # Repair for next test.
    with sqlite3.connect(path) as con:
        con.execute("""
            UPDATE trading_day_state
            SET trades = 1
            WHERE id = 1
        """)


    # --------------------------------------------------------
    # 15. Corrupted count above hard cap fails closed.
    # --------------------------------------------------------

    with sqlite3.connect(path) as con:
        con.execute("""
            UPDATE trading_day_state
            SET trades = 99
            WHERE id = 1
        """)

    ok, reason = g.can_start_trade(
        dt(2026, 9, 22, 13, 0)
    )

    expect(not ok, "Corrupted high count accepted")
    expect(reason == "INVALID_TRADE_COUNT", reason)

    print("15. IMPOSSIBLE PERSISTED COUNT -> BLOCKED: PASS")


    # Repair.
    with sqlite3.connect(path) as con:
        con.execute("""
            UPDATE trading_day_state
            SET trades = 1
            WHERE id = 1
        """)


    # --------------------------------------------------------
    # 16. Fill callback for future date cannot silently reset.
    # --------------------------------------------------------

    ok, reason = g.record_first_fill(
        dt(2026, 9, 23, 9, 31)
    )

    expect(not ok, "Fill silently rolled trading date")
    expect(reason == "DATE_NOT_RECONCILED", reason)

    stored_date, trades = g.snapshot()

    expect(stored_date == "2026-09-22", stored_date)
    expect(trades == 1, trades)

    print("16. FILL CANNOT SILENTLY RESET TRADING DAY: PASS")


    # --------------------------------------------------------
    # 17. Two simultaneous first-fill recordings cannot
    # both bypass the hard daily maximum.
    # Start at one existing trade.
    # --------------------------------------------------------

    import threading

    results = []
    barrier = threading.Barrier(3)

    def record():
        barrier.wait()

        results.append(
            g.record_first_fill(
                dt(2026, 9, 22, 14, 0)
            )
        )

    t1 = threading.Thread(target=record)
    t2 = threading.Thread(target=record)

    t1.start()
    t2.start()

    barrier.wait()

    t1.join()
    t2.join()

    successful = sum(
        1
        for ok, reason in results
        if ok
    )

    expect(
        successful == 1,
        "Concurrent fills bypassed daily cap",
    )

    _, trades = g.snapshot()

    expect(trades == 2, trades)

    print("17. CONCURRENT COUNT UPDATE CANNOT EXCEED 2: PASS")


    # --------------------------------------------------------
    # 18. Final restart still sees count 2.
    # --------------------------------------------------------

    del g

    g = TradingDayGuard(path)

    stored_date, trades = g.snapshot()

    expect(stored_date == "2026-09-22", stored_date)
    expect(trades == 2, trades)

    ok, reason = g.can_start_trade(
        dt(2026, 9, 22, 15, 0)
    )

    expect(not ok, "Final restart bypassed limit")
    expect(reason == "DAILY_LIMIT", reason)

    print("18. FINAL RESTART STILL ENFORCES TWO-TRADE CAP: PASS")


    print()
    print("========================================")
    print("ALL TRADING-DAY SAFETY TESTS PASS")
    print("NEW YORK TRADING DATE ENFORCED")
    print("TWO-TRADE CAP SURVIVES RESTART")
    print("CLOCK ROLLBACK CANNOT RESET COUNT")
    print("CONCURRENT UPDATES CANNOT EXCEED CAP")
    print("CORRUPTED COUNTS FAIL CLOSED")
    print("SIMULATION ONLY")
    print("ZERO IBKR ORDERS")
    print("========================================")
    print()

finally:
    try:
        os.remove(path)
    except OSError:
        pass
