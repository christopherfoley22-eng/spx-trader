from mortificatio_entry import (
    EntryLifecycle,
    EntryLifecycleError,
    chunk_plan,
)


def expect_block(fn, label):
    try:
        fn()
    except EntryLifecycleError:
        return

    raise AssertionError(
        f"UNSAFE ACTION WAS NOT BLOCKED: {label}"
    )


# ------------------------------------------------------------
# CHUNK PLANNING
# ------------------------------------------------------------

assert chunk_plan(1) == [1]
assert chunk_plan(10) == [10]
assert chunk_plan(14) == [10, 4]
assert chunk_plan(20) == [10, 10]
assert chunk_plan(23) == [10, 10, 3]
assert chunk_plan(25) == [10, 10, 5]

assert chunk_plan(25, 5) == [5, 5, 5, 5, 5]


# ------------------------------------------------------------
# NORMAL 25-CONTRACT ENTRY
# ------------------------------------------------------------

entry = EntryLifecycle(
    planned_qty=25,
    con_id=10001,
)

assert entry.next_chunk() == 10

entry.record_fill(10001, 10)

assert entry.filled_qty == 10
assert entry.remaining_qty == 15
assert entry.next_chunk() == 10

entry.record_fill(10001, 10)

assert entry.filled_qty == 20
assert entry.next_chunk() == 5

entry.record_fill(10001, 5)

assert entry.filled_qty == 25
assert entry.remaining_qty == 0
assert entry.finished
assert entry.has_position


# ------------------------------------------------------------
# PARTIAL FIRST ORDER
# planned 10, only 4 actually fill.
# Remaining acquisition gets stopped.
# The 4-contract position MUST survive.
# ------------------------------------------------------------

entry = EntryLifecycle(
    planned_qty=10,
    con_id=20001,
)

entry.record_fill(20001, 4)

assert entry.filled_qty == 4
assert entry.remaining_qty == 6
assert entry.has_position

entry.stop_entry()

assert entry.finished
assert entry.has_position
assert entry.filled_qty == 4


# ------------------------------------------------------------
# PRICE / BUYING POWER CHANGES AFTER FIRST CHUNK
# planned 25, first 10 fill, acquisition stops.
# Existing 10 are still a real trade.
# ------------------------------------------------------------

entry = EntryLifecycle(
    planned_qty=25,
    con_id=30001,
)

entry.record_fill(30001, 10)
entry.stop_entry()

assert entry.finished
assert entry.filled_qty == 10
assert entry.has_position


# ------------------------------------------------------------
# ZERO-FILL REJECTION
# No position exists.
# ------------------------------------------------------------

entry = EntryLifecycle(
    planned_qty=10,
    con_id=40001,
)

entry.stop_entry()

assert entry.finished
assert entry.filled_qty == 0
assert not entry.has_position


# ------------------------------------------------------------
# WRONG CONTRACT FILL MUST NEVER BE ACCEPTED
# ------------------------------------------------------------

entry = EntryLifecycle(
    planned_qty=10,
    con_id=50001,
)

expect_block(
    lambda: entry.record_fill(
        99999,
        1,
    ),
    "wrong-contract fill",
)

assert entry.filled_qty == 0


# ------------------------------------------------------------
# OVERFILL MUST NEVER BE ACCEPTED
# ------------------------------------------------------------

entry = EntryLifecycle(
    planned_qty=10,
    con_id=60001,
)

entry.record_fill(60001, 8)

expect_block(
    lambda: entry.record_fill(
        60001,
        3,
    ),
    "position overfill",
)

assert entry.filled_qty == 8


# ------------------------------------------------------------
# DUPLICATE/LATE FILL AFTER FINISH MUST FAIL
# ------------------------------------------------------------

entry = EntryLifecycle(
    planned_qty=5,
    con_id=70001,
)

entry.record_fill(70001, 5)

expect_block(
    lambda: entry.record_fill(
        70001,
        1,
    ),
    "late fill after completed entry",
)


# ------------------------------------------------------------
# IMPOSSIBLE PLANS
# ------------------------------------------------------------

expect_block(
    lambda: EntryLifecycle(
        planned_qty=0,
        con_id=80001,
    ),
    "zero planned quantity",
)

expect_block(
    lambda: EntryLifecycle(
        planned_qty=26,
        con_id=80001,
    ),
    "planned quantity above 25",
)

expect_block(
    lambda: EntryLifecycle(
        planned_qty=10,
        con_id=-1,
    ),
    "invalid conId",
)


print()
print("=" * 72)
print("ALL MORTIFICATIO ENTRY ATTACK TESTS PASS")
print("10-CONTRACT ENTRY CHUNKING PASS")
print("PARTIAL-FILL OWNERSHIP PASS")
print("BUYING-POWER CHANGE SURVIVAL PASS")
print("ZERO-FILL REJECTION HANDLING PASS")
print("WRONG-CONTRACT / OVERFILL BLOCKING PASS")
print("NO placeOrder() EXISTS")
print("ZERO IBKR ORDERS")
print("=" * 72)
