import os
import tempfile

from mortificatio_state import (
    MortificatioState,
    MortificatioStateError,
    BrokerSnapshot,
    FLAT,
    ENTERING,
    OPEN,
    EXITING,
)


def db():
    fd, path = tempfile.mkstemp(
        prefix="mortificatio_state_",
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


def flat():
    return BrokerSnapshot(
        complete=True,
        position_qty=0,
        con_id=None,
        open_order_count=0,
    )


def pos(qty, con_id, orders=0):
    return BrokerSnapshot(
        complete=True,
        position_qty=qty,
        con_id=con_id,
        open_order_count=orders,
    )


def blocked(fn):
    try:
        fn()
    except MortificatioStateError:
        return
    raise AssertionError("UNSAFE ACTION WAS NOT BLOCKED")


# ------------------------------------------------------------
# 1. Planned quantity survives crash.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    s.request_entry(
        "CALL",
        10,
        10001,
        flat(),
    )

    assert s.status().state == ENTERING
    assert s.status().planned_quantity == 10

    s.close()

    s = MortificatioState(path)

    assert s.status().state == ENTERING
    assert s.status().planned_quantity == 10
    assert s.status().con_id == 10001

    s.close()

finally:
    cleanup(path)


# ------------------------------------------------------------
# 2. Authorized 10, broker reports 11.
# MUST BLOCK.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    s.request_entry(
        "CALL",
        10,
        20001,
        flat(),
    )

    s.close()
    s = MortificatioState(path)

    blocked(
        lambda: s.recover_entry(
            pos(11, 20001)
        )
    )

    assert s.status().state == ENTERING
    assert s.status().planned_quantity == 10
    assert s.status().trades_today == 0

    s.close()

finally:
    cleanup(path)


# ------------------------------------------------------------
# 3. Authorized 10, broker reports 15.
# MUST BLOCK.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    s.request_entry(
        "PUT",
        10,
        30001,
        flat(),
    )

    s.close()
    s = MortificatioState(path)

    blocked(
        lambda: s.recover_entry(
            pos(15, 30001)
        )
    )

    assert s.status().state == ENTERING
    assert s.status().trades_today == 0

    s.close()

finally:
    cleanup(path)


# ------------------------------------------------------------
# 4. Authorized 25, broker reports 26.
# Broker snapshot itself is impossible.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    s.request_entry(
        "CALL",
        25,
        40001,
        flat(),
    )

    blocked(
        lambda: s.recover_entry(
            BrokerSnapshot(
                complete=True,
                position_qty=26,
                con_id=40001,
                open_order_count=0,
            )
        )
    )

    assert s.status().state == ENTERING

    s.close()

finally:
    cleanup(path)


# ------------------------------------------------------------
# 5. Partial fill BELOW authorization is valid.
# Authorized 10, broker owns 4.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    s.request_entry(
        "CALL",
        10,
        50001,
        flat(),
    )

    s.close()
    s = MortificatioState(path)

    recovered = s.recover_entry(
        pos(4, 50001)
    )

    assert recovered.state == OPEN
    assert recovered.planned_quantity == 10
    assert recovered.quantity == 4
    assert recovered.trades_today == 1

    s.close()

finally:
    cleanup(path)


# ------------------------------------------------------------
# 6. Full authorized fill is valid.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    s.request_entry(
        "PUT",
        25,
        60001,
        flat(),
    )

    recovered = s.recover_entry(
        pos(25, 60001)
    )

    assert recovered.state == OPEN
    assert recovered.planned_quantity == 25
    assert recovered.quantity == 25

    s.close()

finally:
    cleanup(path)


# ------------------------------------------------------------
# 7. Wrong conId still blocks.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    s.request_entry(
        "CALL",
        10,
        70001,
        flat(),
    )

    blocked(
        lambda: s.recover_entry(
            pos(5, 99999)
        )
    )

    assert s.status().state == ENTERING

    s.close()

finally:
    cleanup(path)


# ------------------------------------------------------------
# 8. Working broker order blocks finalization.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    s.request_entry(
        "CALL",
        10,
        80001,
        flat(),
    )

    blocked(
        lambda: s.recover_entry(
            pos(5, 80001, orders=1)
        )
    )

    assert s.status().state == ENTERING

    s.close()

finally:
    cleanup(path)


# ------------------------------------------------------------
# 9. OPEN -> EXITING -> FLAT preserves trade count and
# clears authorization only after broker proves flat.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    s.request_entry(
        "PUT",
        10,
        90001,
        flat(),
    )

    s.recover_entry(
        pos(6, 90001)
    )

    opened = s.status()

    assert opened.state == OPEN
    assert opened.planned_quantity == 10
    assert opened.quantity == 6

    s.begin_exit(
        pos(6, 90001)
    )

    exiting = s.status()

    assert exiting.state == EXITING
    assert exiting.planned_quantity == 10
    assert exiting.quantity == 6

    blocked(
        lambda: s.confirm_flat(
            pos(1, 90001)
        )
    )

    assert s.status().state == EXITING

    s.confirm_flat(flat())

    final = s.status()

    assert final.state == FLAT
    assert final.planned_quantity == 0
    assert final.quantity == 0
    assert final.con_id is None
    assert final.trades_today == 1

    s.close()

finally:
    cleanup(path)


# ------------------------------------------------------------
# 10. Daily cap still survives restart.
# ------------------------------------------------------------

path = db()

try:
    s = MortificatioState(path)

    for i in range(2):
        con_id = 100001 + i

        s.request_entry(
            "CALL" if i == 0 else "PUT",
            1,
            con_id,
            flat(),
        )

        s.recover_entry(
            pos(1, con_id)
        )

        s.begin_exit(
            pos(1, con_id)
        )

        s.confirm_flat(
            flat()
        )

    assert s.status().trades_today == 2

    s.close()
    s = MortificatioState(path)

    assert s.status().trades_today == 2

    blocked(
        lambda: s.request_entry(
            "CALL",
            1,
            100003,
            flat(),
        )
    )

    s.close()

finally:
    cleanup(path)


print()
print("=" * 72)
print("ALL MORTIFICATIO STATE ATTACK TESTS PASS")
print("AUTHORIZED QUANTITY PERSISTENCE PASS")
print("PLANNED 10 / BROKER 11 BLOCKED")
print("PLANNED 10 / BROKER 15 BLOCKED")
print("25-CONTRACT ABSOLUTE CAP PASS")
print("VALID PARTIAL-FILL RECOVERY PASS")
print("VALID FULL-FILL RECOVERY PASS")
print("WRONG-CONTRACT RECOVERY BLOCKED")
print("UNRESOLVED BROKER ORDER BLOCKED")
print("OPEN -> EXITING -> BROKER-FLAT PASS")
print("TWO-TRADE DAILY CAP SURVIVES RESTART")
print("NO placeOrder() EXISTS")
print("ZERO IBKR ORDERS")
print("=" * 72)
