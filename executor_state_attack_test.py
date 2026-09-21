import os
import tempfile

from executor_state import (
    ExecutorState,
    BrokerSnapshot,
    SafetyError,
    FLAT,
    ENTERING,
    OPEN,
    EXITING,
)


def expect_block(fn, label):
    try:
        fn()
    except SafetyError:
        return
    raise AssertionError(f"UNSAFE ACTION WAS NOT BLOCKED: {label}")


fd, db_path = tempfile.mkstemp(
    prefix="executor_state_",
    suffix=".db",
)
os.close(fd)

try:
    engine = ExecutorState(db_path)

    broker_flat = BrokerSnapshot(
        complete=True,
        position_qty=0,
        con_id=None,
        open_order_count=0,
    )

    # Initial state
    s = engine.status()
    assert s.state == FLAT
    assert s.quantity == 0
    assert s.trades_today == 0

    # Incomplete broker truth must fail closed.
    expect_block(
        lambda: engine.request_entry(
            "CALL",
            10,
            12345,
            BrokerSnapshot(False, 0, None, 0),
        ),
        "incomplete broker snapshot",
    )

    # Hidden position must block entry.
    expect_block(
        lambda: engine.request_entry(
            "CALL",
            10,
            12345,
            BrokerSnapshot(True, 1, 99999, 0),
        ),
        "hidden broker position",
    )

    # Unknown working order must block entry.
    expect_block(
        lambda: engine.request_entry(
            "CALL",
            10,
            12345,
            BrokerSnapshot(True, 0, None, 1),
        ),
        "broker working order",
    )

    # Valid reservation.
    engine.request_entry(
        "CALL",
        10,
        12345,
        broker_flat,
    )

    assert engine.status().state == ENTERING

    # Duplicate tap must fail.
    expect_block(
        lambda: engine.request_entry(
            "PUT",
            10,
            54321,
            broker_flat,
        ),
        "duplicate/opposite entry tap",
    )

    # Cannot claim fill without broker confirmation.
    expect_block(
        lambda: engine.confirm_entry_fill(
            10,
            12345,
            BrokerSnapshot(True, 0, None, 0),
        ),
        "fake entry fill",
    )

    # Wrong contract must fail.
    expect_block(
        lambda: engine.confirm_entry_fill(
            10,
            12345,
            BrokerSnapshot(True, 10, 77777, 0),
        ),
        "wrong broker contract",
    )

    # Real broker-confirmed fill.
    engine.confirm_entry_fill(
        10,
        12345,
        BrokerSnapshot(True, 10, 12345, 0),
    )

    s = engine.status()
    assert s.state == OPEN
    assert s.quantity == 10
    assert s.trades_today == 1

    # Simulate process death/restart.
    engine.close()
    engine = ExecutorState(db_path)

    s = engine.status()
    assert s.state == OPEN
    assert s.quantity == 10
    assert s.trades_today == 1

    # New entry after restart must remain blocked.
    expect_block(
        lambda: engine.request_entry(
            "PUT",
            5,
            22222,
            broker_flat,
        ),
        "entry while existing trade OPEN",
    )

    # Mismatched broker qty blocks exit transition.
    expect_block(
        lambda: engine.begin_exit(
            BrokerSnapshot(True, 9, 12345, 0)
        ),
        "exit with quantity mismatch",
    )

    # Correct exit transition.
    engine.begin_exit(
        BrokerSnapshot(True, 10, 12345, 0)
    )

    assert engine.status().state == EXITING

    # Duplicate exit trigger blocked.
    expect_block(
        lambda: engine.begin_exit(
            BrokerSnapshot(True, 10, 12345, 0)
        ),
        "duplicate exit",
    )

    # Cannot declare flat while position remains.
    expect_block(
        lambda: engine.confirm_flat(
            BrokerSnapshot(True, 1, 12345, 0)
        ),
        "false flat",
    )

    # Cannot declare flat with unresolved order.
    expect_block(
        lambda: engine.confirm_flat(
            BrokerSnapshot(True, 0, None, 1)
        ),
        "flat with working order",
    )

    # Broker confirms true flat.
    engine.confirm_flat(broker_flat)

    s = engine.status()
    assert s.state == FLAT
    assert s.quantity == 0
    assert s.trades_today == 1

    # Second trade.
    engine.request_entry(
        "PUT",
        5,
        22222,
        broker_flat,
    )

    engine.confirm_entry_fill(
        5,
        22222,
        BrokerSnapshot(True, 5, 22222, 0),
    )

    assert engine.status().trades_today == 2

    engine.begin_exit(
        BrokerSnapshot(True, 5, 22222, 0)
    )

    engine.confirm_flat(broker_flat)

    # Third trade MUST be impossible.
    expect_block(
        lambda: engine.request_entry(
            "CALL",
            1,
            33333,
            broker_flat,
        ),
        "third daily trade",
    )

    # Impossible quantities blocked.
    expect_block(
        lambda: engine.request_entry(
            "CALL",
            0,
            33333,
            broker_flat,
        ),
        "zero quantity",
    )

    expect_block(
        lambda: engine.request_entry(
            "CALL",
            26,
            33333,
            broker_flat,
        ),
        "quantity above 25",
    )

    print()
    print("=" * 72)
    print("ALL EXECUTOR STATE ATTACK TESTS PASS")
    print("CRASH/RESTART PERSISTENCE PASS")
    print("BROKER RECONCILIATION GATES PASS")
    print("DUPLICATE CALL/PUT AND EXIT BLOCKED")
    print("TWO-TRADE DAILY HARD CAP PASS")
    print("ZERO IBKR ORDERS")
    print("=" * 72)

finally:
    try:
        engine.close()
    except Exception:
        pass

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass
