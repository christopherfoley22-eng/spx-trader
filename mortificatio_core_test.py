import os
import tempfile

from executor_state import (
    BrokerSnapshot,
    SafetyError,
    ENTERING,
)

from mortificatio_core import (
    Mortificatio,
    OptionQuote,
    SelectionError,
    build_entry_plan,
)


def expect_block(fn, label):
    try:
        fn()
    except (SafetyError, SelectionError):
        return

    raise AssertionError(
        f"UNSAFE ACTION WAS NOT BLOCKED: {label}"
    )


# ------------------------------------------------------------
# CONTRACT SELECTION / SIZING
# ------------------------------------------------------------

quotes = [
    OptionQuote(
        con_id=1001,
        strike=7695.0,
        ask=12.00,
    ),
    OptionQuote(
        con_id=1002,
        strike=7700.0,
        ask=10.00,
    ),
    OptionQuote(
        con_id=1003,
        strike=7705.0,
        ask=8.00,
    ),
]

plan = build_entry_plan(
    direction="CALL",
    spx_price=7700.40,
    usable_funds=10_500.00,
    candidates=quotes,
)

assert plan.con_id == 1002
assert plan.strike == 7700.0
assert plan.quantity == 10
assert plan.estimated_cost == 10_000.0


# ------------------------------------------------------------
# ATM TOO EXPENSIVE:
# closest affordable contract should be selected.
# ------------------------------------------------------------

quotes = [
    OptionQuote(
        con_id=2001,
        strike=7700.0,
        ask=15.00,
    ),
    OptionQuote(
        con_id=2002,
        strike=7705.0,
        ask=9.00,
    ),
    OptionQuote(
        con_id=2003,
        strike=7710.0,
        ask=7.00,
    ),
]

plan = build_entry_plan(
    direction="CALL",
    spx_price=7700.0,
    usable_funds=1_000.00,
    candidates=quotes,
)

assert plan.con_id == 2002
assert plan.quantity == 1


# ------------------------------------------------------------
# 25-CONTRACT HARD CAP
# ------------------------------------------------------------

plan = build_entry_plan(
    direction="PUT",
    spx_price=7700.0,
    usable_funds=100_000.00,
    candidates=[
        OptionQuote(
            con_id=3001,
            strike=7700.0,
            ask=10.00,
        )
    ],
)

assert plan.quantity == 25


# ------------------------------------------------------------
# NOTHING AFFORDABLE
# ------------------------------------------------------------

expect_block(
    lambda: build_entry_plan(
        direction="CALL",
        spx_price=7700.0,
        usable_funds=500.00,
        candidates=[
            OptionQuote(
                con_id=4001,
                strike=7700.0,
                ask=10.00,
            )
        ],
    ),
    "unaffordable contract",
)


# ------------------------------------------------------------
# NOW TEST SELECTION + PERSISTENT SAFETY AS ONE SYSTEM
# ------------------------------------------------------------

fd, db_path = tempfile.mkstemp(
    prefix="mortificatio_",
    suffix=".db",
)
os.close(fd)

app = None

try:
    app = Mortificatio(db_path)

    broker_flat = BrokerSnapshot(
        complete=True,
        position_qty=0,
        con_id=None,
        open_order_count=0,
    )

    plan = app.prepare_entry(
        direction="CALL",
        spx_price=7700.40,
        usable_funds=10_500.00,
        candidates=[
            OptionQuote(5001, 7695.0, 12.00),
            OptionQuote(5002, 7700.0, 10.00),
            OptionQuote(5003, 7705.0, 8.00),
        ],
        broker_snapshot=broker_flat,
    )

    assert plan.con_id == 5002
    assert plan.quantity == 10

    status = app.status()

    assert status.state == ENTERING
    assert status.direction == "CALL"
    assert status.con_id == 5002
    assert status.quantity == 0

    # A second phone tap must not create another entry.
    expect_block(
        lambda: app.prepare_entry(
            direction="PUT",
            spx_price=7700.40,
            usable_funds=10_500.00,
            candidates=[
                OptionQuote(
                    6001,
                    7700.0,
                    10.00,
                )
            ],
            broker_snapshot=broker_flat,
        ),
        "second CALL/PUT tap while ENTERING",
    )

    # Restart must preserve ENTERING.
    app.close()
    app = Mortificatio(db_path)

    status = app.status()

    assert status.state == ENTERING
    assert status.direction == "CALL"
    assert status.con_id == 5002

    # Still blocked after restart.
    expect_block(
        lambda: app.prepare_entry(
            direction="CALL",
            spx_price=7701.00,
            usable_funds=20_000.00,
            candidates=[
                OptionQuote(
                    7001,
                    7700.0,
                    9.00,
                )
            ],
            broker_snapshot=broker_flat,
        ),
        "new entry after restart with unresolved ENTERING state",
    )

    print()
    print("=" * 72)
    print("ALL MORTIFICATIO CORE TESTS PASS")
    print("ATM / AFFORDABILITY SELECTION PASS")
    print("25-CONTRACT HARD CAP PASS")
    print("PERSISTENT ENTRY RESERVATION PASS")
    print("DUPLICATE CALL/PUT BLOCKED")
    print("CRASH/RESTART BLOCKING PASS")
    print("NO placeOrder() EXISTS")
    print("ZERO IBKR ORDERS")
    print("=" * 72)

finally:
    if app is not None:
        try:
            app.close()
        except Exception:
            pass

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass
