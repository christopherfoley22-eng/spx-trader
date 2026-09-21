"""
MORTIFICATIO full lifecycle crash/restart attack suite.

SIMULATION / PERSISTENT-STATE TESTING ONLY.
NO IBKR CONNECTION.
NO placeOrder().
ZERO ORDERS.

Goal:
Prove that persistent local state + broker truth fail closed across
critical crash points in the lifecycle.
"""

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

from mortificatio_recovery import (
    recover_entering_position,
    RecoveryError,
)


def make_db():
    fd, path = tempfile.mkstemp(
        prefix="mortificatio_crash_",
        suffix=".db",
    )
    os.close(fd)
    return path


def cleanup(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def restart(state, db_path):
    state.close()
    return ExecutorState(db_path)


def expect_block(fn, label):
    try:
        fn()
    except (SafetyError, RecoveryError):
        return

    raise AssertionError(
        f"UNSAFE ACTION WAS NOT BLOCKED: {label}"
    )


def flat_snapshot():
    return BrokerSnapshot(
        complete=True,
        position_qty=0,
        con_id=None,
        open_order_count=0,
    )


def position_snapshot(qty, con_id, open_orders=0):
    return BrokerSnapshot(
        complete=True,
        position_qty=qty,
        con_id=con_id,
        open_order_count=open_orders,
    )


# ============================================================
# 1. CRASH WHILE COMPLETELY FLAT
#
# Restart should remain FLAT.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    assert state.status().state == FLAT

    state = restart(state, db)

    status = state.status()

    assert status.state == FLAT
    assert status.quantity == 0
    assert status.con_id is None
    assert status.trades_today == 0

    state.close()

finally:
    cleanup(db)


# ============================================================
# 2. CRASH IMMEDIATELY AFTER ENTRY RESERVATION
#
# No broker position.
# Local state must remain ENTERING.
# It must NOT silently become FLAT or OPEN.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="CALL",
        planned_qty=10,
        planned_con_id=10001,
        broker=flat_snapshot(),
    )

    state = restart(state, db)

    status = state.status()

    assert status.state == ENTERING
    assert status.quantity == 0
    assert status.con_id == 10001
    assert status.trades_today == 0

    expect_block(
        lambda: recover_entering_position(
            state,
            flat_snapshot(),
        ),
        "inventing fill after reservation-only crash",
    )

    expect_block(
        lambda: state.request_entry(
            direction="PUT",
            planned_qty=10,
            planned_con_id=10002,
            broker=flat_snapshot(),
        ),
        "second entry after reservation crash",
    )

    state.close()

finally:
    cleanup(db)


# ============================================================
# 3. CRASH AFTER ONE CONTRACT FILLS
#
# This is a real position.
# Restart must adopt OPEN 1 and count exactly one trade.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="CALL",
        planned_qty=10,
        planned_con_id=20001,
        broker=flat_snapshot(),
    )

    state = restart(state, db)

    broker = position_snapshot(
        qty=1,
        con_id=20001,
    )

    recovered = recover_entering_position(
        state,
        broker,
    )

    assert recovered.state == OPEN
    assert recovered.quantity == 1
    assert recovered.trades_today == 1

    state = restart(state, db)

    status = state.status()

    assert status.state == OPEN
    assert status.quantity == 1
    assert status.trades_today == 1

    state.close()

finally:
    cleanup(db)


# ============================================================
# 4. CRASH AFTER FIRST 10 OF PLANNED 25 FILL
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="PUT",
        planned_qty=25,
        planned_con_id=30001,
        broker=flat_snapshot(),
    )

    state = restart(state, db)

    broker = position_snapshot(
        qty=10,
        con_id=30001,
    )

    recovered = recover_entering_position(
        state,
        broker,
    )

    assert recovered.state == OPEN
    assert recovered.quantity == 10
    assert recovered.trades_today == 1

    state.close()

finally:
    cleanup(db)


# ============================================================
# 5. CRASH WITH PARTIAL POSITION + WORKING ENTRY ORDER
#
# Example:
# 10 contracts filled and another chunk is still working.
#
# Recovery MUST NOT finalize while broker activity is unresolved.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="CALL",
        planned_qty=25,
        planned_con_id=40001,
        broker=flat_snapshot(),
    )

    state = restart(state, db)

    unresolved = position_snapshot(
        qty=10,
        con_id=40001,
        open_orders=1,
    )

    expect_block(
        lambda: recover_entering_position(
            state,
            unresolved,
        ),
        "finalizing entry while order remains working",
    )

    status = state.status()

    assert status.state == ENTERING
    assert status.trades_today == 0

    state.close()

finally:
    cleanup(db)


# ============================================================
# 6. CRASH IMMEDIATELY AFTER OPEN IS PERSISTED
#
# Restart must remain OPEN and must NOT count another trade.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="CALL",
        planned_qty=10,
        planned_con_id=50001,
        broker=flat_snapshot(),
    )

    broker = position_snapshot(
        qty=10,
        con_id=50001,
    )

    state.confirm_entry_fill(
        filled_qty=10,
        con_id=50001,
        broker=broker,
    )

    assert state.status().trades_today == 1

    state = restart(state, db)

    status = state.status()

    assert status.state == OPEN
    assert status.quantity == 10
    assert status.trades_today == 1

    expect_block(
        lambda: recover_entering_position(
            state,
            broker,
        ),
        "double recovery of already OPEN trade",
    )

    assert state.status().trades_today == 1

    state.close()

finally:
    cleanup(db)


# ============================================================
# 7. CRASH BEFORE EXIT STATE IS PERSISTED
#
# Broker still owns position.
# Local state remains OPEN.
# After restart, broker truth agrees, so exit may begin.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="PUT",
        planned_qty=8,
        planned_con_id=60001,
        broker=flat_snapshot(),
    )

    broker_open = position_snapshot(
        qty=8,
        con_id=60001,
    )

    state.confirm_entry_fill(
        filled_qty=8,
        con_id=60001,
        broker=broker_open,
    )

    state = restart(state, db)

    assert state.status().state == OPEN

    state.begin_exit(
        broker=broker_open,
    )

    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 8. CRASH IMMEDIATELY AFTER EXITING IS PERSISTED
#
# Broker still owns all contracts.
# Restart must remain EXITING.
# New entry must be impossible.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="CALL",
        planned_qty=7,
        planned_con_id=70001,
        broker=flat_snapshot(),
    )

    broker_open = position_snapshot(
        qty=7,
        con_id=70001,
    )

    state.confirm_entry_fill(
        filled_qty=7,
        con_id=70001,
        broker=broker_open,
    )

    state.begin_exit(
        broker=broker_open,
    )

    state = restart(state, db)

    status = state.status()

    assert status.state == EXITING
    assert status.quantity == 7
    assert status.trades_today == 1

    expect_block(
        lambda: state.request_entry(
            direction="PUT",
            planned_qty=5,
            planned_con_id=70002,
            broker=flat_snapshot(),
        ),
        "entry while persistent state is EXITING",
    )

    state.close()

finally:
    cleanup(db)


# ============================================================
# 9. PARTIAL EXIT EXISTS AT BROKER
#
# Local state says EXITING qty 10.
# Broker says only 4 remain.
#
# Current state engine MUST NOT falsely declare FLAT.
#
# NOTE:
# Updating EXITING quantity to broker-confirmed remaining quantity
# will belong to the future live exit-order/reconciliation layer.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="PUT",
        planned_qty=10,
        planned_con_id=80001,
        broker=flat_snapshot(),
    )

    broker_open = position_snapshot(
        qty=10,
        con_id=80001,
    )

    state.confirm_entry_fill(
        filled_qty=10,
        con_id=80001,
        broker=broker_open,
    )

    state.begin_exit(
        broker=broker_open,
    )

    state = restart(state, db)

    broker_partial_exit = position_snapshot(
        qty=4,
        con_id=80001,
    )

    expect_block(
        lambda: state.confirm_flat(
            broker_partial_exit,
        ),
        "declaring FLAT after partial exit",
    )

    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 10. BROKER BECOMES FLAT, THEN PROCESS CRASHES BEFORE LOCAL
# confirm_flat() IS RECORDED.
#
# Restart local state remains EXITING.
# Broker truth is FLAT.
# confirm_flat() safely completes lifecycle.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="CALL",
        planned_qty=6,
        planned_con_id=90001,
        broker=flat_snapshot(),
    )

    broker_open = position_snapshot(
        qty=6,
        con_id=90001,
    )

    state.confirm_entry_fill(
        filled_qty=6,
        con_id=90001,
        broker=broker_open,
    )

    state.begin_exit(
        broker=broker_open,
    )

    # Imagine exit fills at IBKR here.
    # Then MORTIFICATIO crashes before confirm_flat().

    state = restart(state, db)

    assert state.status().state == EXITING

    state.confirm_flat(
        broker=flat_snapshot(),
    )

    status = state.status()

    assert status.state == FLAT
    assert status.quantity == 0
    assert status.con_id is None
    assert status.trades_today == 1

    state.close()

finally:
    cleanup(db)


# ============================================================
# 11. CRASH AFTER FLAT IS PERSISTED
#
# Restart must remain FLAT while preserving daily trade count.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="CALL",
        planned_qty=5,
        planned_con_id=100001,
        broker=flat_snapshot(),
    )

    broker_open = position_snapshot(
        qty=5,
        con_id=100001,
    )

    state.confirm_entry_fill(
        filled_qty=5,
        con_id=100001,
        broker=broker_open,
    )

    state.begin_exit(
        broker=broker_open,
    )

    state.confirm_flat(
        broker=flat_snapshot(),
    )

    state = restart(state, db)

    status = state.status()

    assert status.state == FLAT
    assert status.quantity == 0
    assert status.trades_today == 1

    state.close()

finally:
    cleanup(db)


# ============================================================
# 12. TWO COMPLETED TRADES + CRASH
#
# Restart must still block trade #3.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    # Trade 1
    state.request_entry(
        direction="CALL",
        planned_qty=2,
        planned_con_id=110001,
        broker=flat_snapshot(),
    )

    broker1 = position_snapshot(
        qty=2,
        con_id=110001,
    )

    state.confirm_entry_fill(
        filled_qty=2,
        con_id=110001,
        broker=broker1,
    )

    state.begin_exit(broker1)
    state.confirm_flat(flat_snapshot())

    # Trade 2
    state.request_entry(
        direction="PUT",
        planned_qty=3,
        planned_con_id=110002,
        broker=flat_snapshot(),
    )

    broker2 = position_snapshot(
        qty=3,
        con_id=110002,
    )

    state.confirm_entry_fill(
        filled_qty=3,
        con_id=110002,
        broker=broker2,
    )

    state.begin_exit(broker2)
    state.confirm_flat(flat_snapshot())

    assert state.status().trades_today == 2

    state = restart(state, db)

    assert state.status().trades_today == 2

    expect_block(
        lambda: state.request_entry(
            direction="CALL",
            planned_qty=1,
            planned_con_id=110003,
            broker=flat_snapshot(),
        ),
        "third trade after crash/restart",
    )

    state.close()

finally:
    cleanup(db)


# ============================================================
# 13. WRONG BROKER CONTRACT WHILE LOCAL STATE IS OPEN
#
# Never begin exit against a different contract.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="CALL",
        planned_qty=5,
        planned_con_id=120001,
        broker=flat_snapshot(),
    )

    correct = position_snapshot(
        qty=5,
        con_id=120001,
    )

    state.confirm_entry_fill(
        filled_qty=5,
        con_id=120001,
        broker=correct,
    )

    state = restart(state, db)

    wrong = position_snapshot(
        qty=5,
        con_id=999999,
    )

    expect_block(
        lambda: state.begin_exit(
            broker=wrong,
        ),
        "exit against wrong broker contract",
    )

    assert state.status().state == OPEN

    state.close()

finally:
    cleanup(db)


# ============================================================
# 14. INCOMPLETE BROKER DATA DURING EXIT
#
# Fail closed.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    state.request_entry(
        direction="PUT",
        planned_qty=5,
        planned_con_id=130001,
        broker=flat_snapshot(),
    )

    broker_open = position_snapshot(
        qty=5,
        con_id=130001,
    )

    state.confirm_entry_fill(
        filled_qty=5,
        con_id=130001,
        broker=broker_open,
    )

    incomplete = BrokerSnapshot(
        complete=False,
        position_qty=5,
        con_id=130001,
        open_order_count=0,
    )

    expect_block(
        lambda: state.begin_exit(
            broker=incomplete,
        ),
        "exit transition with incomplete broker snapshot",
    )

    assert state.status().state == OPEN

    state.close()

finally:
    cleanup(db)


print()
print("=" * 72)
print("ALL MORTIFICATIO LIFECYCLE CRASH TESTS PASS")
print("FLAT RESTART PASS")
print("ENTRY-RESERVATION CRASH PASS")
print("1-CONTRACT PARTIAL-FILL RECOVERY PASS")
print("CHUNKED PARTIAL-FILL RECOVERY PASS")
print("UNRESOLVED ENTRY ORDER FAIL-CLOSED PASS")
print("OPEN RESTART / NO DOUBLE COUNT PASS")
print("EXIT-START CRASH PASS")
print("EXITING RESTART PASS")
print("PARTIAL EXIT CANNOT FALSELY BECOME FLAT")
print("BROKER-FLAT / LOCAL-EXITING RECOVERY PASS")
print("DAILY TRADE CAP SURVIVES RESTART")
print("WRONG-CONTRACT EXIT BLOCKED")
print("INCOMPLETE BROKER DATA FAIL-CLOSED PASS")
print("NO placeOrder() EXISTS")
print("ZERO IBKR ORDERS")
print("=" * 72)
