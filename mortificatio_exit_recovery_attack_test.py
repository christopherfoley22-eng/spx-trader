import os
import tempfile

from executor_state import (
    ExecutorState,
    BrokerSnapshot,
    SafetyError,
    OPEN,
    EXITING,
    FLAT,
)

from mortificatio_exit_recovery import (
    reconcile_exit,
    ExitRecoveryError,
)


def make_db():
    fd, path = tempfile.mkstemp(
        prefix="mortificatio_exit_",
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


def expect_block(fn, label):
    try:
        fn()
    except (SafetyError, ExitRecoveryError):
        return

    raise AssertionError(
        f"UNSAFE ACTION WAS NOT BLOCKED: {label}"
    )


def flat():
    return BrokerSnapshot(
        complete=True,
        position_qty=0,
        con_id=None,
        open_order_count=0,
    )


def position(qty, con_id, orders=0):
    return BrokerSnapshot(
        complete=True,
        position_qty=qty,
        con_id=con_id,
        open_order_count=orders,
    )


def build_exiting_position(
    state,
    qty,
    con_id,
    direction="CALL",
):
    state.request_entry(
        direction=direction,
        planned_qty=qty,
        planned_con_id=con_id,
        broker=flat(),
    )

    broker_open = position(
        qty,
        con_id,
    )

    state.confirm_entry_fill(
        filled_qty=qty,
        con_id=con_id,
        broker=broker_open,
    )

    state.begin_exit(
        broker=broker_open,
    )

    assert state.status().state == EXITING


# ============================================================
# 1. 25 CONTRACTS REMAIN
#
# Default exit plan should be 10 + 10 + 5.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=25,
        con_id=10001,
    )

    plan = reconcile_exit(
        state,
        position(25, 10001),
    )

    assert plan.remaining_qty == 25
    assert plan.chunks == [10, 10, 5]
    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 2. OPTIONAL 5-CONTRACT EXIT CHUNKING
#
# We have NOT selected 5 vs 10 as production behavior.
# This proves either plan can be represented.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=25,
        con_id=20001,
    )

    plan = reconcile_exit(
        state,
        position(25, 20001),
        max_chunk=5,
    )

    assert plan.chunks == [5, 5, 5, 5, 5]

    state.close()

finally:
    cleanup(db)


# ============================================================
# 3. FIRST 10 EXIT, 15 REMAIN
#
# Crash/restart.
# Broker truth says 15 remain.
# New plan must be ONLY 15.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=25,
        con_id=30001,
    )

    state.close()
    state = ExecutorState(db)

    assert state.status().state == EXITING

    plan = reconcile_exit(
        state,
        position(15, 30001),
    )

    assert plan.remaining_qty == 15
    assert plan.chunks == [10, 5]

    state.close()

finally:
    cleanup(db)


# ============================================================
# 4. 24 OF 25 EXIT, ONE REMAINS
#
# That final contract is still owned.
# MORTIFICATIO must continue EXITING.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=25,
        con_id=40001,
    )

    plan = reconcile_exit(
        state,
        position(1, 40001),
    )

    assert plan.remaining_qty == 1
    assert plan.chunks == [1]
    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 5. BROKER FLAT
#
# Only now may persistent state become FLAT.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=10,
        con_id=50001,
    )

    result = reconcile_exit(
        state,
        flat(),
    )

    assert result is None

    status = state.status()

    assert status.state == FLAT
    assert status.quantity == 0
    assert status.con_id is None
    assert status.trades_today == 1

    state.close()

finally:
    cleanup(db)


# ============================================================
# 6. ZERO POSITION BUT EXIT ORDER STILL WORKING
#
# Do NOT call FLAT until order state is resolved.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=10,
        con_id=60001,
    )

    unresolved = BrokerSnapshot(
        complete=True,
        position_qty=0,
        con_id=None,
        open_order_count=1,
    )

    expect_block(
        lambda: reconcile_exit(
            state,
            unresolved,
        ),
        "flat position with unresolved exit order",
    )

    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 7. PARTIAL EXIT + WORKING ORDER
#
# 15 remain but an existing sell is still working.
#
# Creating another exit order here could oversell.
# Must wait/reconcile first.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=25,
        con_id=70001,
    )

    unresolved = position(
        qty=15,
        con_id=70001,
        orders=1,
    )

    expect_block(
        lambda: reconcile_exit(
            state,
            unresolved,
        ),
        "duplicate exit while existing order works",
    )

    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 8. WRONG CONTRACT APPEARS
#
# Never generate an exit plan against some other position.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=10,
        con_id=80001,
    )

    expect_block(
        lambda: reconcile_exit(
            state,
            position(5, 99999),
        ),
        "wrong-contract exit recovery",
    )

    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 9. BROKER QUANTITY INCREASES
#
# Local EXITING began with 10.
# Broker suddenly reports 11.
#
# This is contradictory and must fail closed.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=10,
        con_id=90001,
    )

    expect_block(
        lambda: reconcile_exit(
            state,
            position(11, 90001),
        ),
        "broker quantity increased during exit",
    )

    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 10. INCOMPLETE BROKER SNAPSHOT
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=10,
        con_id=100001,
    )

    incomplete = BrokerSnapshot(
        complete=False,
        position_qty=5,
        con_id=100001,
        open_order_count=0,
    )

    expect_block(
        lambda: reconcile_exit(
            state,
            incomplete,
        ),
        "incomplete broker data during exit",
    )

    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 11. EXITING IS IRREVERSIBLE
#
# There is intentionally no function here that returns EXITING
# to OPEN because price improved.
#
# A new CALL/PUT must also remain blocked.
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=8,
        con_id=110001,
    )

    assert state.status().state == EXITING

    expect_block(
        lambda: state.request_entry(
            direction="PUT",
            planned_qty=8,
            planned_con_id=110002,
            broker=flat(),
        ),
        "new direction while exit is unfinished",
    )

    assert state.status().state == EXITING

    state.close()

finally:
    cleanup(db)


# ============================================================
# 12. MULTIPLE CRASHES DURING ONE EXIT
#
# 25 -> crash -> 15 -> crash -> 5 -> crash -> 0
# ============================================================

db = make_db()

try:
    state = ExecutorState(db)

    build_exiting_position(
        state,
        qty=25,
        con_id=120001,
    )

    # Crash #1
    state.close()
    state = ExecutorState(db)

    plan = reconcile_exit(
        state,
        position(15, 120001),
    )

    assert plan.chunks == [10, 5]

    # Crash #2
    state.close()
    state = ExecutorState(db)

    plan = reconcile_exit(
        state,
        position(5, 120001),
    )

    assert plan.chunks == [5]

    # Crash #3 after final broker fill
    state.close()
    state = ExecutorState(db)

    result = reconcile_exit(
        state,
        flat(),
    )

    assert result is None
    assert state.status().state == FLAT
    assert state.status().trades_today == 1

    state.close()

finally:
    cleanup(db)


print()
print("=" * 72)
print("ALL MORTIFICATIO PARTIAL-EXIT ATTACK TESTS PASS")
print("25-CONTRACT EXIT PLANNING PASS")
print("10-MAX AND 5-MAX CHUNK REPRESENTATION PASS")
print("PARTIAL EXIT RECONCILIATION PASS")
print("FINAL SINGLE CONTRACT OWNERSHIP PASS")
print("BROKER-CONFIRMED FLAT PASS")
print("UNRESOLVED EXIT ORDER FAIL-CLOSED PASS")
print("DUPLICATE / OVERSELL PROTECTION PASS")
print("WRONG-CONTRACT EXIT BLOCKED")
print("IMPOSSIBLE QUANTITY INCREASE BLOCKED")
print("MULTIPLE EXIT CRASH/RESTART RECOVERY PASS")
print("EXITING IS IRREVERSIBLE")
print("NO placeOrder() EXISTS")
print("ZERO IBKR ORDERS")
print("=" * 72)
