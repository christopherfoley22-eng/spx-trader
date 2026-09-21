import threading
from dataclasses import dataclass
from enum import Enum


MAX_CONTRACTS = 25


class State(Enum):
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"


class Direction(Enum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass
class Position:
    direction: Direction
    target_qty: int
    filled_qty: int = 0


class SafeLifecycle:
    def __init__(self):
        self._lock = threading.Lock()
        self.state = State.FLAT
        self.position = None
        self.trades_today = 0
        self.exit_reason = None

    def request_entry(self, direction, qty):
        with self._lock:
            if self.state != State.FLAT:
                return False, "NOT_FLAT"

            if self.trades_today >= 2:
                return False, "DAILY_LIMIT"

            if direction not in (Direction.CALL, Direction.PUT):
                return False, "INVALID_DIRECTION"

            if (
                not isinstance(qty, int)
                or isinstance(qty, bool)
                or qty < 1
                or qty > MAX_CONTRACTS
            ):
                return False, "INVALID_QTY"

            # Atomic reservation:
            # state changes before another request can enter.
            self.state = State.ENTERING
            self.position = Position(direction, qty)

            return True, "ENTRY_RESERVED"

    def confirm_entry_fill(self, qty):
        with self._lock:
            if self.state != State.ENTERING:
                return False, "NOT_ENTERING"

            if (
                not isinstance(qty, int)
                or isinstance(qty, bool)
                or qty <= 0
            ):
                return False, "INVALID_FILL_QTY"

            if self.position is None:
                return False, "MISSING_POSITION"

            if (
                self.position.filled_qty + qty
                > self.position.target_qty
            ):
                return False, "FILL_EXCEEDS_TARGET"

            first_fill = self.position.filled_qty == 0

            self.position.filled_qty += qty

            # A trade counts when the first actual entry fill occurs.
            if first_fill:
                self.trades_today += 1

            self.state = State.OPEN

            return True, "ENTRY_FILL_CONFIRMED"

    def request_exit(self, reason):
        with self._lock:
            if self.state == State.EXITING:
                return False, "EXIT_ALREADY_ACTIVE"

            if self.state != State.OPEN:
                return False, "NOT_OPEN"

            if (
                self.position is None
                or self.position.filled_qty <= 0
            ):
                return False, "NO_CONFIRMED_POSITION"

            # First exit trigger wins permanently.
            self.state = State.EXITING
            self.exit_reason = reason

            return True, "EXIT_RESERVED"

    def confirm_exit_fill(self, qty):
        with self._lock:
            if self.state != State.EXITING:
                return False, "NOT_EXITING"

            if (
                not isinstance(qty, int)
                or isinstance(qty, bool)
                or qty <= 0
            ):
                return False, "INVALID_FILL_QTY"

            if self.position is None:
                return False, "MISSING_POSITION"

            if qty > self.position.filled_qty:
                return False, "EXIT_EXCEEDS_OWNED"

            self.position.filled_qty -= qty

            if self.position.filled_qty == 0:
                self.position = None
                self.state = State.FLAT
                self.exit_reason = None
                return True, "FLAT_CONFIRMED"

            return True, "PARTIAL_EXIT_CONFIRMED"

    def disconnect(self):
        # A disconnect NEVER mutates ownership/state.
        # Unknown broker truth must be reconciled separately.
        with self._lock:
            return self.state


def expect(x, msg):
    assert x, msg


print()
print("========================================")
print("LIFECYCLE / RACE-CONDITION ATTACK TESTS")
print("========================================")


# ------------------------------------------------------------
# 1. Double-tap CALLS simultaneously.
# Exactly one request may reserve entry.
# ------------------------------------------------------------

engine = SafeLifecycle()
results = []
barrier = threading.Barrier(3)


def call_request():
    barrier.wait()
    results.append(
        engine.request_entry(Direction.CALL, 10)
    )


t1 = threading.Thread(target=call_request)
t2 = threading.Thread(target=call_request)

t1.start()
t2.start()
barrier.wait()

t1.join()
t2.join()

accepted = sum(1 for ok, _ in results if ok)

expect(accepted == 1, f"Accepted {accepted} entries")
expect(engine.state == State.ENTERING, "Must be ENTERING")

print("1. SIMULTANEOUS DOUBLE-TAP -> ONE ENTRY: PASS")


# ------------------------------------------------------------
# 2. CALL and PUT hit simultaneously.
# Exactly one direction wins.
# ------------------------------------------------------------

engine = SafeLifecycle()
results = []
barrier = threading.Barrier(3)


def request(direction):
    barrier.wait()
    results.append(
        (direction, engine.request_entry(direction, 10))
    )


t1 = threading.Thread(
    target=request,
    args=(Direction.CALL,),
)
t2 = threading.Thread(
    target=request,
    args=(Direction.PUT,),
)

t1.start()
t2.start()
barrier.wait()

t1.join()
t2.join()

accepted = [
    direction
    for direction, (ok, _) in results
    if ok
]

expect(len(accepted) == 1, "Exactly one direction must win")
expect(engine.position.direction == accepted[0], "Direction mismatch")

print("2. CALL/PUT COLLISION -> ONE DIRECTION: PASS")


# ------------------------------------------------------------
# 3. Entry fill counted once as a trade.
# ------------------------------------------------------------

ok, reason = engine.confirm_entry_fill(4)

expect(ok, reason)
expect(engine.trades_today == 1, "Trade count must be 1")
expect(engine.position.filled_qty == 4, "Must own 4")

print("3. FIRST ACTUAL FILL COUNTS TRADE: PASS")


# ------------------------------------------------------------
# 4. A second entry request while OPEN is blocked.
# ------------------------------------------------------------

ok, reason = engine.request_entry(Direction.CALL, 5)

expect(not ok, "Entry while OPEN was accepted")
expect(reason == "NOT_FLAT", reason)

print("4. ENTRY WHILE OPEN BLOCKED: PASS")


# ------------------------------------------------------------
# 5. Two exit triggers fire simultaneously.
# Exactly one can own the exit transition.
# ------------------------------------------------------------

results = []
barrier = threading.Barrier(3)


def exit_request(reason):
    barrier.wait()
    results.append(
        (reason, engine.request_exit(reason))
    )


t1 = threading.Thread(
    target=exit_request,
    args=("TRAIL_STOP",),
)
t2 = threading.Thread(
    target=exit_request,
    args=("SECOND_TRIGGER",),
)

t1.start()
t2.start()
barrier.wait()

t1.join()
t2.join()

accepted_exits = [
    reason
    for reason, (ok, _) in results
    if ok
]

expect(len(accepted_exits) == 1, "Multiple exits accepted")
expect(engine.state == State.EXITING, "Must be EXITING")
expect(
    engine.exit_reason == accepted_exits[0],
    "First accepted exit reason must remain authoritative",
)

print("5. SIMULTANEOUS EXIT TRIGGERS -> ONE EXIT: PASS")


# ------------------------------------------------------------
# 6. Entry request during EXITING is blocked.
# ------------------------------------------------------------

ok, reason = engine.request_entry(Direction.PUT, 5)

expect(not ok, "Entry during EXITING accepted")
expect(reason == "NOT_FLAT", reason)

print("6. ENTRY DURING EXITING BLOCKED: PASS")


# ------------------------------------------------------------
# 7. Partial exit does not falsely declare FLAT.
# ------------------------------------------------------------

ok, reason = engine.confirm_exit_fill(2)

expect(ok, reason)
expect(reason == "PARTIAL_EXIT_CONFIRMED", reason)
expect(engine.state == State.EXITING, "Must remain EXITING")
expect(engine.position.filled_qty == 2, "Must still own 2")

print("7. PARTIAL EXIT REMAINS EXITING: PASS")


# ------------------------------------------------------------
# 8. Disconnect during EXITING cannot make us flat.
# ------------------------------------------------------------

state_before = engine.state
returned_state = engine.disconnect()

expect(state_before == State.EXITING, "Expected EXITING")
expect(returned_state == State.EXITING, "Disconnect mutated state")
expect(engine.state == State.EXITING, "Disconnect made state unsafe")
expect(engine.position.filled_qty == 2, "Ownership changed")

print("8. DISCONNECT CANNOT ERASE POSITION: PASS")


# ------------------------------------------------------------
# 9. Oversell is blocked.
# ------------------------------------------------------------

ok, reason = engine.confirm_exit_fill(3)

expect(not ok, "Oversell accepted")
expect(reason == "EXIT_EXCEEDS_OWNED", reason)
expect(engine.position.filled_qty == 2, "Ownership changed")

print("9. OVERSELL DURING EXIT BLOCKED: PASS")


# ------------------------------------------------------------
# 10. Exact remaining exit reaches FLAT.
# ------------------------------------------------------------

ok, reason = engine.confirm_exit_fill(2)

expect(ok, reason)
expect(reason == "FLAT_CONFIRMED", reason)
expect(engine.state == State.FLAT, "Must be FLAT")
expect(engine.position is None, "Position must be cleared")

print("10. CONFIRMED ZERO OWNERSHIP -> FLAT: PASS")


# ------------------------------------------------------------
# 11. Exit callback after already FLAT is rejected.
# ------------------------------------------------------------

ok, reason = engine.confirm_exit_fill(1)

expect(not ok, "Exit fill while flat accepted")
expect(reason == "NOT_EXITING", reason)

print("11. LATE EXIT CALLBACK WHILE FLAT BLOCKED: PASS")


# ------------------------------------------------------------
# 12. New second trade can start only after confirmed flat.
# ------------------------------------------------------------

ok, reason = engine.request_entry(Direction.PUT, 7)

expect(ok, reason)
expect(engine.state == State.ENTERING, "Must be ENTERING")

print("12. SECOND TRADE AFTER CONFIRMED FLAT: PASS")


# ------------------------------------------------------------
# 13. First partial fill counts second trade.
# ------------------------------------------------------------

ok, reason = engine.confirm_entry_fill(3)

expect(ok, reason)
expect(engine.trades_today == 2, "Trade count must be 2")

print("13. SECOND TRADE COUNTED ON FIRST FILL: PASS")


# ------------------------------------------------------------
# 14. Third trade cannot start even after second trade exits.
# ------------------------------------------------------------

ok, reason = engine.request_exit("TEST_EXIT")
expect(ok, reason)

ok, reason = engine.confirm_exit_fill(3)
expect(ok, reason)
expect(engine.state == State.FLAT, "Must be flat")

ok, reason = engine.request_entry(Direction.CALL, 1)

expect(not ok, "Third trade accepted")
expect(reason == "DAILY_LIMIT", reason)

print("14. THIRD DAILY TRADE BLOCKED: PASS")


# ------------------------------------------------------------
# 15. Disconnect during ENTERING cannot reset reservation.
# ------------------------------------------------------------

fresh = SafeLifecycle()

ok, reason = fresh.request_entry(Direction.CALL, 5)
expect(ok, reason)

fresh.disconnect()

expect(fresh.state == State.ENTERING, "Reservation disappeared")

ok, reason = fresh.request_entry(Direction.PUT, 5)

expect(not ok, "Second entry accepted after disconnect")
expect(reason == "NOT_FLAT", reason)

print("15. DISCONNECT DURING ENTRY PRESERVES LOCK: PASS")


# ------------------------------------------------------------
# 16. Disconnect while OPEN cannot erase ownership.
# ------------------------------------------------------------

ok, reason = fresh.confirm_entry_fill(5)
expect(ok, reason)

fresh.disconnect()

expect(fresh.state == State.OPEN, "OPEN state disappeared")
expect(fresh.position.filled_qty == 5, "Position disappeared")

print("16. DISCONNECT WHILE OPEN PRESERVES POSITION: PASS")


# ------------------------------------------------------------
# 17. Two threads attempting final exit fill cannot both sell.
# ------------------------------------------------------------

ok, reason = fresh.request_exit("FINAL")
expect(ok, reason)

results = []
barrier = threading.Barrier(3)


def final_fill():
    barrier.wait()
    results.append(
        fresh.confirm_exit_fill(5)
    )


t1 = threading.Thread(target=final_fill)
t2 = threading.Thread(target=final_fill)

t1.start()
t2.start()
barrier.wait()

t1.join()
t2.join()

successful = sum(1 for ok, _ in results if ok)

expect(successful == 1, f"{successful} final fills accepted")
expect(fresh.state == State.FLAT, "Must finish FLAT")
expect(fresh.position is None, "Position must be zero")

print("17. COMPETING FINAL EXIT FILLS -> ONE ACCEPTED: PASS")


print()
print("========================================")
print("ALL LIFECYCLE / RACE ATTACK TESTS PASS")
print("ENTRY RESERVATION IS ATOMIC")
print("CALL/PUT COLLISIONS CANNOT DOUBLE-ENTER")
print("FIRST EXIT TRANSITION WINS")
print("DISCONNECTS DO NOT ERASE OWNERSHIP")
print("THIRD DAILY TRADE BLOCKED")
print("SIMULATION ONLY")
print("ZERO IBKR ORDERS")
print("========================================")
print()
