from enum import Enum, auto


# ============================================================
# LOCKED STRATEGY PARAMETERS
# ============================================================

INITIAL_STOP = 3.25

NEAR_WINNER_ARM = 4.00
NEAR_WINNER_FLOOR = 1.00

LET_IT_RIDE_ARM = 5.00
LET_IT_RIDE_TRAIL = 3.00


class Direction(Enum):
    CALL = 1
    PUT = -1


class State(Enum):
    INITIAL = auto()
    NEAR_WINNER = auto()
    LET_IT_RIDE = auto()
    CLOSED = auto()


class Trade:
    def __init__(self, direction: Direction, entry_spx: float):
        self.direction = direction
        self.entry_spx = float(entry_spx)

        self.state = State.INITIAL
        self.max_favorable = 0.0

        self.exit_reason = None
        self.exit_move = None

    def favorable_move(self, spx: float) -> float:
        """
        Positive = trade moving in our favor.
        Negative = trade moving against us.
        """
        return (float(spx) - self.entry_spx) * self.direction.value

    def update(self, spx: float):
        if self.state == State.CLOSED:
            return None

        move = self.favorable_move(spx)

        # High-water mark can NEVER move backward.
        if move > self.max_favorable:
            self.max_favorable = move

        # ----------------------------------------------------
        # STATE TRANSITIONS
        # ----------------------------------------------------

        # +5 always has priority over +4.
        if (
            self.state in (State.INITIAL, State.NEAR_WINNER)
            and self.max_favorable >= LET_IT_RIDE_ARM
        ):
            self.state = State.LET_IT_RIDE

        elif (
            self.state == State.INITIAL
            and self.max_favorable >= NEAR_WINNER_ARM
        ):
            self.state = State.NEAR_WINNER

        # ----------------------------------------------------
        # EXIT RULES
        # ----------------------------------------------------

        if self.state == State.INITIAL:
            if move <= -INITIAL_STOP:
                return self._exit(
                    "INITIAL_STOP",
                    move,
                )

        elif self.state == State.NEAR_WINNER:
            if move <= NEAR_WINNER_FLOOR:
                return self._exit(
                    "NEAR_WINNER_FLOOR",
                    move,
                )

        elif self.state == State.LET_IT_RIDE:
            trailing_floor = self.max_favorable - LET_IT_RIDE_TRAIL

            if move <= trailing_floor:
                return self._exit(
                    "LET_IT_RIDE_TRAIL",
                    move,
                )

        return None

    def _exit(self, reason, move):
        self.state = State.CLOSED
        self.exit_reason = reason
        self.exit_move = move

        return {
            "action": "EXIT_ALL",
            "reason": reason,
            "move": move,
            "peak": self.max_favorable,
        }


# ============================================================
# TEST HELPERS
# ============================================================

def call_trade():
    return Trade(Direction.CALL, 7700.0)


def put_trade():
    return Trade(Direction.PUT, 7700.0)


# ============================================================
# ATTACK TESTS
# ============================================================

# 1. CALL: 3.24 adverse must HOLD.
t = call_trade()
assert t.update(7696.76) is None
assert t.state == State.INITIAL


# 2. CALL: exactly 3.25 adverse must EXIT.
t = call_trade()
result = t.update(7696.75)
assert result is not None
assert result["reason"] == "INITIAL_STOP"
assert t.state == State.CLOSED


# 3. PUT: mirrored 3.25 adverse stop.
t = put_trade()
result = t.update(7703.25)
assert result is not None
assert result["reason"] == "INITIAL_STOP"


# 4. +3.99 does NOT arm near-winner protection.
t = call_trade()
assert t.update(7703.99) is None
assert t.state == State.INITIAL

assert t.update(7701.00) is None
assert t.state == State.INITIAL


# 5. Exactly +4.00 arms near-winner protection.
t = call_trade()
assert t.update(7704.00) is None
assert t.state == State.NEAR_WINNER


# 6. Once +4 armed, +1.01 still HOLDS.
t = call_trade()
t.update(7704.00)
assert t.update(7701.01) is None
assert t.state == State.NEAR_WINNER


# 7. Once +4 armed, exactly +1.00 EXITS.
t = call_trade()
t.update(7704.00)
result = t.update(7701.00)

assert result is not None
assert result["reason"] == "NEAR_WINNER_FLOOR"


# 8. User's +4.8 example.
t = call_trade()

t.update(7704.80)
assert t.state == State.NEAR_WINNER

assert t.update(7702.00) is None

result = t.update(7701.00)
assert result is not None
assert result["reason"] == "NEAR_WINNER_FLOOR"


# 9. +4.8 can recover and reach +5.
t = call_trade()

t.update(7704.80)
assert t.state == State.NEAR_WINNER

t.update(7702.00)
assert t.state == State.NEAR_WINNER

t.update(7705.00)
assert t.state == State.LET_IT_RIDE


# 10. Exactly +5 activates LET IT RIDE.
t = call_trade()
assert t.update(7705.00) is None
assert t.state == State.LET_IT_RIDE
assert t.max_favorable == 5.00


# 11. Peak +10, 2.99 reversal = HOLD.
t = call_trade()

t.update(7705.00)
t.update(7710.00)

assert t.max_favorable == 10.00

assert t.update(7707.01) is None
assert t.state == State.LET_IT_RIDE


# 12. Peak +10, exactly 3.00 reversal = EXIT.
t = call_trade()

t.update(7705.00)
t.update(7710.00)

result = t.update(7707.00)

assert result is not None
assert result["reason"] == "LET_IT_RIDE_TRAIL"
assert result["peak"] == 10.00


# 13. Screenshot-style +13.5 winner.
t = call_trade()

t.update(7705.00)
t.update(7708.00)
t.update(7711.00)
t.update(7713.50)

assert t.max_favorable == 13.50

# 2.99-point pullback: still alive.
assert t.update(7710.51) is None

# 3.00-point pullback: EXIT.
result = t.update(7710.50)

assert result is not None
assert result["reason"] == "LET_IT_RIDE_TRAIL"


# 14. New highs must move trailing floor forward.
t = call_trade()

t.update(7705.00)
t.update(7710.00)
t.update(7713.50)
t.update(7716.25)

assert t.max_favorable == 16.25

# Old floor would no longer matter.
assert t.update(7713.26) is None

result = t.update(7713.25)

assert result is not None
assert result["reason"] == "LET_IT_RIDE_TRAIL"


# 15. Huge jump through +4 and +5 goes directly to LET IT RIDE.
t = call_trade()

t.update(7706.25)

assert t.state == State.LET_IT_RIDE
assert t.max_favorable == 6.25


# 16. Gap THROUGH the 3-point trail must still exit.
t = call_trade()

t.update(7710.00)

result = t.update(7705.50)

assert result is not None
assert result["reason"] == "LET_IT_RIDE_TRAIL"


# 17. Closed trade must ignore all future ticks.
t = call_trade()

t.update(7710.00)
t.update(7707.00)

assert t.state == State.CLOSED

assert t.update(7725.00) is None
assert t.state == State.CLOSED


# 18. PUT: +4 arms near-winner.
t = put_trade()

t.update(7696.00)

assert t.state == State.NEAR_WINNER


# 19. PUT: +4 then reverse to +1 = EXIT.
t = put_trade()

t.update(7696.00)

result = t.update(7699.00)

assert result is not None
assert result["reason"] == "NEAR_WINNER_FLOOR"


# 20. PUT: +5 activates LET IT RIDE.
t = put_trade()

t.update(7695.00)

assert t.state == State.LET_IT_RIDE


# 21. PUT: peak +10, 2.99 reversal = HOLD.
t = put_trade()

t.update(7695.00)
t.update(7690.00)

assert t.update(7692.99) is None


# 22. PUT: peak +10, 3.00 reversal = EXIT.
t = put_trade()

t.update(7695.00)
t.update(7690.00)

result = t.update(7693.00)

assert result is not None
assert result["reason"] == "LET_IT_RIDE_TRAIL"


print()
print("ALL LET-IT-RIDE ATTACK TESTS PASS")
print("INITIAL STOP:       -3.25")
print("NEAR-WINNER ARM:    +4.00")
print("NEAR-WINNER FLOOR:  +1.00")
print("LET-IT-RIDE ARM:    +5.00")
print("LET-IT-RIDE TRAIL:   3.00")
print("SIMULATION ONLY - ZERO IBKR ORDERS")
print()
