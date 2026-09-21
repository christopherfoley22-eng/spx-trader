import os
import tempfile

from mortificatio_strategy_state import (
    DurableStrategyState,
    StrategyStateError,
)


def new_db():
    fd, path = tempfile.mkstemp(
        prefix="mortificatio_strategy_",
        suffix=".db",
    )
    os.close(fd)
    os.unlink(path)
    return path


def reopen(path):
    return DurableStrategyState(path)


def expect_error(fn):
    try:
        fn()
    except StrategyStateError:
        return
    raise AssertionError("Expected StrategyStateError")


# ============================================================
# 1. CALL: +12 survives crash, +9 exits
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 111, 7700.0)

d = s.process_spx(7712.0)
assert d.action == "HOLD"

snap = s.status()
assert snap.let_it_ride_armed
assert snap.best_spx == 7712.0
s.close()

s = reopen(path)
snap = s.status()
assert snap.let_it_ride_armed
assert snap.best_spx == 7712.0

d = s.process_spx(7709.0)
assert d.action == "EXIT"
assert d.reason == "LET_IT_RIDE_REVERSAL"

s.close()
os.remove(path)
print("CALL +12 CRASH -> +9 EXIT PASS")


# ============================================================
# 2. PUT mirror: +12 survives crash, +9 exits
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("PUT", 222, 7700.0)

d = s.process_spx(7688.0)
assert d.action == "HOLD"

s.close()
s = reopen(path)

snap = s.status()
assert snap.let_it_ride_armed
assert snap.best_spx == 7688.0

d = s.process_spx(7691.0)
assert d.action == "EXIT"
assert d.reason == "LET_IT_RIDE_REVERSAL"

s.close()
os.remove(path)
print("PUT +12 CRASH -> +9 EXIT PASS")


# ============================================================
# 3. Exact +5 arms and survives restart
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 333, 7700.0)

d = s.process_spx(7705.0)
assert d.action == "HOLD"
assert s.status().let_it_ride_armed

s.close()
s = reopen(path)

assert s.status().let_it_ride_armed

d = s.process_spx(7702.0)
assert d.action == "EXIT"
assert d.reason == "LET_IT_RIDE_REVERSAL"

s.close()
os.remove(path)
print("EXACT +5 ARM SURVIVES RESTART PASS")


# ============================================================
# 4. 2.99 reversal must NOT exit
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 444, 7700.0)
s.process_spx(7710.0)

d = s.process_spx(7707.01)
assert d.action == "HOLD"

s.close()
os.remove(path)
print("2.99 REVERSAL HOLDS PASS")


# ============================================================
# 5. Exact 3.00 reversal exits
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 555, 7700.0)
s.process_spx(7710.0)

d = s.process_spx(7707.0)
assert d.action == "EXIT"
assert d.reason == "LET_IT_RIDE_REVERSAL"

s.close()
os.remove(path)
print("EXACT 3.00 REVERSAL EXITS PASS")


# ============================================================
# 6. Initial -3.25 CALL stop
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 666, 7700.0)

d = s.process_spx(7696.75)
assert d.action == "EXIT"
assert d.reason == "INITIAL_STOP"

s.close()
os.remove(path)
print("CALL INITIAL -3.25 STOP PASS")


# ============================================================
# 7. Initial -3.25 PUT stop
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("PUT", 777, 7700.0)

d = s.process_spx(7703.25)
assert d.action == "EXIT"
assert d.reason == "INITIAL_STOP"

s.close()
os.remove(path)
print("PUT INITIAL -3.25 STOP PASS")


# ============================================================
# 8. Gap-through records observed reality
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 888, 7700.0)

d = s.process_spx(7694.0)
assert d.action == "EXIT"
assert d.reason == "INITIAL_STOP"
assert d.favorable_points == -6.0

s.close()
os.remove(path)
print("GAP-THROUGH OBSERVED PRICE PASS")


# ============================================================
# 9. +4.8 reversal remains intentionally unresolved
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 999, 7700.0)

d = s.process_spx(7704.8)
assert d.action == "HOLD"
assert not s.status().let_it_ride_armed

d = s.process_spx(7700.5)
assert d.action == "HOLD"
assert d.reason is None

s.close()
os.remove(path)
print("PRE-+5 WINNER PROTECTION REMAINS UNRESOLVED PASS")


# ============================================================
# 10. Exit intent survives crash and cannot revert to HOLD
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 1010, 7700.0)
s.process_spx(7715.0)

d = s.process_spx(7712.0)
assert d.action == "EXIT"

s.close()
s = reopen(path)

snap = s.status()
assert snap.exit_required
assert snap.exit_reason == "LET_IT_RIDE_REVERSAL"

# Even if price immediately rockets higher after restart,
# the prior exit decision is irreversible.
d = s.process_spx(7725.0)
assert d.action == "EXIT"
assert d.reason == "LET_IT_RIDE_REVERSAL"

s.close()
os.remove(path)
print("DURABLE EXIT INTENT IS IRREVERSIBLE PASS")


# ============================================================
# 11. Cannot clear strategy before exit intent
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 1111, 7700.0)

expect_error(
    lambda: s.clear_after_broker_flat()
)

s.close()
os.remove(path)
print("PREMATURE STRATEGY CLEAR BLOCKED")


# ============================================================
# 12. Clear only after exit has become required
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 1212, 7700.0)
s.process_spx(7696.75)

assert s.status().exit_required

final = s.clear_after_broker_flat()
assert not final.active

s.close()
os.remove(path)
print("POST-EXIT BROKER-FLAT CLEAR PASS")


# ============================================================
# 13. Cannot activate second strategy over active trade
# ============================================================

path = new_db()
s = DurableStrategyState(path)
s.activate("CALL", 1313, 7700.0)

expect_error(
    lambda: s.activate("PUT", 1414, 7700.0)
)

s.close()
os.remove(path)
print("DUPLICATE STRATEGY ACTIVATION BLOCKED")


# ============================================================
# 14. Source must contain no order-submission capability
# ============================================================

source = open(
    "mortificatio_strategy_state.py",
    "r",
).read()

forbidden = "place" + "Order"
assert forbidden not in source

print("NO IBKR ORDER SUBMISSION EXISTS")
print("ZERO IBKR ORDERS")

print()
print("=" * 72)
print("ALL DURABLE STRATEGY STATE ATTACK TESTS PASS")
print("=" * 72)
