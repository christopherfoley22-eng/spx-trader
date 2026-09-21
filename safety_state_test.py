import sqlite3
import tempfile
from pathlib import Path

MAX_CONTRACTS = 25
DAILY_TRADE_LIMIT = 2

FLAT = "FLAT"
ENTERING = "ENTERING"
OPEN = "OPEN"
EXITING = "EXITING"


class SafetyState:
    def __init__(self, db_path):
        self.db = sqlite3.connect(db_path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                status TEXT NOT NULL,
                direction TEXT,
                quantity INTEGER NOT NULL,
                trades_today INTEGER NOT NULL
            )
        """)
        self.db.execute("""
            INSERT OR IGNORE INTO state
            (id, status, direction, quantity, trades_today)
            VALUES (1, 'FLAT', NULL, 0, 0)
        """)
        self.db.commit()

    def read(self):
        row = self.db.execute("""
            SELECT status, direction, quantity, trades_today
            FROM state WHERE id = 1
        """).fetchone()

        return {
            "status": row[0],
            "direction": row[1],
            "quantity": row[2],
            "trades_today": row[3],
        }

    def request_entry(self, direction, quantity):
        s = self.read()

        if direction not in ("CALL", "PUT"):
            return False, "INVALID DIRECTION"

        if quantity < 1 or quantity > MAX_CONTRACTS:
            return False, "INVALID QUANTITY"

        if s["status"] != FLAT:
            return False, "POSITION/ORDER ALREADY ACTIVE"

        if s["trades_today"] >= DAILY_TRADE_LIMIT:
            return False, "DAILY TRADE LIMIT REACHED"

        self.db.execute("""
            UPDATE state
            SET status=?, direction=?, quantity=?
            WHERE id=1
        """, (ENTERING, direction, quantity))
        self.db.commit()

        return True, "ENTRY RESERVED"

    def confirm_entry(self, actual_quantity):
        s = self.read()

        if s["status"] != ENTERING:
            return False, "NOT ENTERING"

        if actual_quantity < 1 or actual_quantity > MAX_CONTRACTS:
            return False, "INVALID ACTUAL POSITION"

        self.db.execute("""
            UPDATE state
            SET status=?, quantity=?,
                trades_today=trades_today+1
            WHERE id=1
        """, (OPEN, actual_quantity))
        self.db.commit()

        return True, "POSITION OPEN"

    def begin_exit(self):
        s = self.read()

        if s["status"] == EXITING:
            return False, "EXIT ALREADY IN PROGRESS"

        if s["status"] != OPEN:
            return False, "NO OPEN POSITION TO EXIT"

        self.db.execute("""
            UPDATE state SET status=? WHERE id=1
        """, (EXITING,))
        self.db.commit()

        return True, "EXIT RESERVED"

    def confirm_flat(self):
        s = self.read()

        if s["status"] != EXITING:
            return False, "CANNOT ASSUME FLAT"

        self.db.execute("""
            UPDATE state
            SET status=?, direction=NULL, quantity=0
            WHERE id=1
        """, (FLAT,))
        self.db.commit()

        return True, "FLAT CONFIRMED"

    def close(self):
        self.db.close()


def assert_state(engine, status, qty, trades):
    s = engine.read()
    assert s["status"] == status
    assert s["quantity"] == qty
    assert s["trades_today"] == trades


with tempfile.TemporaryDirectory() as tmp:
    db_path = Path(tmp) / "executor_test.db"

    print("\n1. NORMAL ENTRY")
    e = SafetyState(db_path)

    ok, msg = e.request_entry("CALL", 25)
    print(ok, msg)
    assert ok
    assert_state(e, ENTERING, 25, 0)

    print("\n2. DUPLICATE TAP WHILE ENTERING")
    ok, msg = e.request_entry("CALL", 25)
    print(ok, msg)
    assert not ok

    print("\n3. OPPOSITE BUTTON WHILE ENTERING")
    ok, msg = e.request_entry("PUT", 25)
    print(ok, msg)
    assert not ok

    print("\n4. CONFIRM ACTUAL POSITION")
    ok, msg = e.confirm_entry(23)
    print(ok, msg)
    assert ok
    assert_state(e, OPEN, 23, 1)

    print("\n5. NEW ENTRY WHILE POSITION OPEN")
    ok, msg = e.request_entry("PUT", 10)
    print(ok, msg)
    assert not ok

    print("\n6. SIMULATED CRASH")
    e.close()

    print("Executor process killed.")

    print("\n7. RESTART FROM SAME DATABASE")
    e = SafetyState(db_path)
    s = e.read()
    print(s)

    # CRITICAL:
    # Restart must remember the position.
    assert_state(e, OPEN, 23, 1)

    print("\n8. ENTRY AFTER RESTART MUST STILL BE BLOCKED")
    ok, msg = e.request_entry("CALL", 5)
    print(ok, msg)
    assert not ok

    print("\n9. BEGIN EXIT")
    ok, msg = e.begin_exit()
    print(ok, msg)
    assert ok
    assert_state(e, EXITING, 23, 1)

    print("\n10. DUPLICATE EXIT")
    ok, msg = e.begin_exit()
    print(ok, msg)
    assert not ok

    print("\n11. CRASH DURING EXIT")
    e.close()

    print("Executor process killed during liquidation.")

    print("\n12. RESTART DURING EXIT")
    e = SafetyState(db_path)
    s = e.read()
    print(s)

    # Must remember that liquidation was in progress.
    assert_state(e, EXITING, 23, 1)

    print("\n13. CANNOT ENTER DURING RECOVERY")
    ok, msg = e.request_entry("PUT", 25)
    print(ok, msg)
    assert not ok

    print("\n14. CONFIRM FLAT")
    ok, msg = e.confirm_flat()
    print(ok, msg)
    assert ok
    assert_state(e, FLAT, 0, 1)

    print("\n15. SECOND TRADE")
    ok, msg = e.request_entry("PUT", 10)
    print(ok, msg)
    assert ok

    ok, msg = e.confirm_entry(10)
    print(ok, msg)
    assert ok

    ok, msg = e.begin_exit()
    assert ok

    ok, msg = e.confirm_flat()
    assert ok

    assert_state(e, FLAT, 0, 2)

    print("\n16. THIRD TRADE MUST BE BLOCKED")
    ok, msg = e.request_entry("CALL", 1)
    print(ok, msg)
    assert not ok
    assert msg == "DAILY TRADE LIMIT REACHED"

    print("\n17. IMPOSSIBLE QUANTITIES")
    # Daily cap would also block entry now, so directly verify
    # that quantity validation happens first.
    for qty in (0, -1, 26, 100, 1000000):
        ok, msg = e.request_entry("CALL", qty)
        print(qty, ok, msg)
        assert not ok
        assert msg == "INVALID QUANTITY"

    e.close()

print("\n========================================")
print("ALL PERSISTENT SAFETY TESTS PASS")
print("CRASH/RESTART STATE SURVIVED")
print("DUPLICATE ENTRY/EXIT BLOCKED")
print("THIRD DAILY TRADE BLOCKED")
print("ZERO IBKR ORDERS")
print("========================================")
