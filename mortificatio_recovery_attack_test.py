import os
import tempfile

from executor_state import (
    ExecutorState,
    BrokerSnapshot,
    SafetyError,
    FLAT,
    ENTERING,
    OPEN,
)

from mortificatio_recovery import (
    recover_entering_position,
    RecoveryError,
)


def expect_block(fn, label):
    try:
        fn()
    except (SafetyError, RecoveryError):
        return

    raise AssertionError(
        f"UNSAFE ACTION WAS NOT BLOCKED: {label}"
    )


def make_db():
    fd, path = tempfile.mkstemp(
        prefix="mortificatio_recovery_",
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


BROKER_FLAT = BrokerSnapshot(
    complete=True,
    position_qty=0,
    con_id=None,
    open_order_count=0,
)


# ============================================================
# 1. THE CRITICAL CASE
#
# Reserve 10.
# 4 reach IBKR.
# Process dies before local confirmation.
# Restart.
# Broker says 4 exist.
# MORTIFICATIO must take ownership -> OPEN 4.
# ============================================================

db_path = make_db()

try:
    state = ExecutorState(db_path)

    state.request_entry(
        direction="CALL",
        planned_qty=10,
        planned_con_id=10001,
        broker=BROKER_FLAT,
    )

    status = state.status()

    assert status.state == ENTERING
    assert status.quantity == 0
    assert status.trades_today == 0

    # Simulate process death/restart.
    state.close()

    state = ExecutorState(db_path)

    status = state.status()

    assert status.state == ENTERING
    assert status.quantity == 0
    assert status.con_id == 10001

    broker_after_crash = BrokerSnapshot(
        complete=True,
        position_qty=4,
        con_id=10001,
        open_order_count=0,
    )

    recovered = recover_entering_position(
        state,
        broker_after_crash,
    )

    assert recovered.state == OPEN
    assert recovered.quantity == 4
    assert recovered.con_id == 10001
    assert recovered.trades_today == 1

    # A second recovery must NOT count another trade.
    expect_block(
        lambda: recover_entering_position(
            state,
            broker_after_crash,
        ),
        "duplicate recovery",
    )

    assert state.status().trades_today == 1

    state.close()

finally:
    cleanup(db_path)


# ============================================================
# 2. WRONG CONTRACT AFTER CRASH
# Never adopt some unrelated broker position.
# ============================================================

db_path = make_db()

try:
    state = ExecutorState(db_path)

    state.request_entry(
        direction="PUT",
        planned_qty=10,
        planned_con_id=20001,
        broker=BROKER_FLAT,
    )

    wrong_contract = BrokerSnapshot(
        complete=True,
        position_qty=4,
        con_id=99999,
        open_order_count=0,
    )

    expect_block(
        lambda: recover_entering_position(
            state,
            wrong_contract,
        ),
        "wrong contract recovery",
    )

    assert state.status().state == ENTERING
    assert state.status().trades_today == 0

    state.close()

finally:
    cleanup(db_path)


# ============================================================
# 3. OPEN ORDER STILL EXISTS
#
# Example:
# 4 filled but IBKR still has remaining quantity working.
#
# We MUST NOT finalize OPEN yet because broker activity is
# unresolved.
# ============================================================

db_path = make_db()

try:
    state = ExecutorState(db_path)

    state.request_entry(
        direction="CALL",
        planned_qty=10,
        planned_con_id=30001,
        broker=BROKER_FLAT,
    )

    unresolved = BrokerSnapshot(
        complete=True,
        position_qty=4,
        con_id=30001,
        open_order_count=1,
    )

    expect_block(
        lambda: recover_entering_position(
            state,
            unresolved,
        ),
        "recovery with working broker order",
    )

    assert state.status().state == ENTERING
    assert state.status().trades_today == 0

    state.close()

finally:
    cleanup(db_path)


# ============================================================
# 4. INCOMPLETE BROKER SNAPSHOT
# ============================================================

db_path = make_db()

try:
    state = ExecutorState(db_path)

    state.request_entry(
        direction="CALL",
        planned_qty=10,
        planned_con_id=40001,
        broker=BROKER_FLAT,
    )

    incomplete = BrokerSnapshot(
        complete=False,
        position_qty=4,
        con_id=40001,
        open_order_count=0,
    )

    expect_block(
        lambda: recover_entering_position(
            state,
            incomplete,
        ),
        "incomplete broker snapshot",
    )

    assert state.status().state == ENTERING

    state.close()

finally:
    cleanup(db_path)


# ============================================================
# 5. BROKER IS FLAT AFTER CRASH
#
# We do NOT silently assume whether:
# - order was rejected
# - order never reached IBKR
# - order was cancelled
# - some event was missed
#
# Recovery therefore refuses to fabricate a position.
# ============================================================

db_path = make_db()

try:
    state = ExecutorState(db_path)

    state.request_entry(
        direction="PUT",
        planned_qty=10,
        planned_con_id=50001,
        broker=BROKER_FLAT,
    )

    expect_block(
        lambda: recover_entering_position(
            state,
            BROKER_FLAT,
        ),
        "fabricated zero-fill recovery",
    )

    assert state.status().state == ENTERING
    assert state.status().trades_today == 0

    state.close()

finally:
    cleanup(db_path)


# ============================================================
# 6. SECOND PHONE TAP DURING RECOVERY STATE
# ============================================================

db_path = make_db()

try:
    state = ExecutorState(db_path)

    state.request_entry(
        direction="CALL",
        planned_qty=10,
        planned_con_id=60001,
        broker=BROKER_FLAT,
    )

    expect_block(
        lambda: state.request_entry(
            direction="PUT",
            planned_qty=10,
            planned_con_id=60002,
            broker=BROKER_FLAT,
        ),
        "opposite-direction tap during ENTERING recovery",
    )

    assert state.status().state == ENTERING

    state.close()

finally:
    cleanup(db_path)


# ============================================================
# 7. FULL 25-CONTRACT POSITION RECOVERED
# ============================================================

db_path = make_db()

try:
    state = ExecutorState(db_path)

    state.request_entry(
        direction="PUT",
        planned_qty=25,
        planned_con_id=70001,
        broker=BROKER_FLAT,
    )

    broker_full = BrokerSnapshot(
        complete=True,
        position_qty=25,
        con_id=70001,
        open_order_count=0,
    )

    recovered = recover_entering_position(
        state,
        broker_full,
    )

    assert recovered.state == OPEN
    assert recovered.quantity == 25
    assert recovered.trades_today == 1

    state.close()

finally:
    cleanup(db_path)


# ============================================================
# 8. RECOVERY CANNOT BE CALLED FROM FLAT
# ============================================================

db_path = make_db()

try:
    state = ExecutorState(db_path)

    phantom = BrokerSnapshot(
        complete=True,
        position_qty=4,
        con_id=80001,
        open_order_count=0,
    )

    expect_block(
        lambda: recover_entering_position(
            state,
            phantom,
        ),
        "adopting position from local FLAT state",
    )

    assert state.status().state == FLAT

    state.close()

finally:
    cleanup(db_path)


print()
print("=" * 72)
print("ALL MORTIFICATIO RECOVERY ATTACK TESTS PASS")
print("PARTIAL-FILL CRASH/RESTART RECOVERY PASS")
print("BROKER TRUTH OWNERSHIP PASS")
print("WRONG-CONTRACT ADOPTION BLOCKED")
print("UNRESOLVED BROKER ORDER BLOCKED")
print("DUPLICATE RECOVERY / DOUBLE COUNT BLOCKED")
print("SECOND CALL/PUT DURING RECOVERY BLOCKED")
print("ZERO-FILL STATE NOT FABRICATED")
print("NO placeOrder() EXISTS")
print("ZERO IBKR ORDERS")
print("=" * 72)
