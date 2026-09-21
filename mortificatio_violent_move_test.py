"""
MORTIFICATIO violent-move attack harness.

SIMULATION ONLY.
NO IBKR CONNECTION.
NO ORDER SUBMISSION.

Tests:
- CALL and PUT symmetry
- jumps through the initial -3.25 stop
- instant +5 activation
- huge favorable springs
- 3.00-point peak reversal
- jumps through the reversal threshold
- exactly-at-threshold behavior
- unresolved pre-+5 reversals
"""

INITIAL_STOP = 3.25
LET_IT_RIDE_START = 5.00
PEAK_REVERSAL = 3.00

CALL = "CALL"
PUT = "PUT"

HOLD = "HOLD"
EXIT_INITIAL_STOP = "EXIT_INITIAL_STOP"
EXIT_PEAK_REVERSAL = "EXIT_PEAK_REVERSAL"


class ViolentMoveEngine:
    def __init__(self, direction):
        if direction not in {CALL, PUT}:
            raise ValueError("Direction must be CALL or PUT")

        self.direction = direction
        self.max_favorable = 0.0
        self.let_it_ride = False
        self.closed = False
        self.exit_reason = None

    def favorable_points(self, spx_move):
        """
        spx_move is movement from entry:
          + = SPX rose
          - = SPX fell

        CALL benefits from positive SPX movement.
        PUT benefits from negative SPX movement.
        """
        if self.direction == CALL:
            return spx_move

        return -spx_move

    def on_tick(self, spx_move):
        if self.closed:
            return self.exit_reason

        favorable = self.favorable_points(spx_move)

        # Initial losing stop applies before +5 has established
        # Let It Ride.
        if not self.let_it_ride and favorable <= -INITIAL_STOP:
            self.closed = True
            self.exit_reason = EXIT_INITIAL_STOP
            return self.exit_reason

        # Track best favorable excursion.
        if favorable > self.max_favorable:
            self.max_favorable = favorable

        # +5 or beyond immediately arms Let It Ride.
        if self.max_favorable >= LET_IT_RIDE_START:
            self.let_it_ride = True

        # Once armed, a 3-point reversal from the best observed
        # favorable excursion exits everything.
        if self.let_it_ride:
            reversal = self.max_favorable - favorable

            if reversal >= PEAK_REVERSAL:
                self.closed = True
                self.exit_reason = EXIT_PEAK_REVERSAL
                return self.exit_reason

        return HOLD


def run(direction, path):
    engine = ViolentMoveEngine(direction)

    events = []

    for move in path:
        action = engine.on_tick(move)

        events.append(
            (
                move,
                engine.favorable_points(move),
                engine.max_favorable,
                engine.let_it_ride,
                action,
            )
        )

        if engine.closed:
            break

    return engine, events


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# ============================================================
# 1. CALL: violent losing jump THROUGH -3.25
# We may observe -2 and then -6.
# The engine must exit on the -6 observation.
# It must NOT pretend the fill occurred at -3.25.
# ============================================================

engine, events = run(
    CALL,
    [0, -1, -2, -6],
)

require(engine.closed, "CALL gap loser did not exit")
require(
    engine.exit_reason == EXIT_INITIAL_STOP,
    "CALL gap loser used wrong exit",
)


# ============================================================
# 2. PUT: mirrored violent losing jump
# SPX rising hurts the PUT.
# ============================================================

engine, events = run(
    PUT,
    [0, 1, 2, 6],
)

require(engine.closed, "PUT gap loser did not exit")
require(
    engine.exit_reason == EXIT_INITIAL_STOP,
    "PUT gap loser used wrong exit",
)


# ============================================================
# 3. Exact initial-stop threshold
# ============================================================

engine, events = run(
    CALL,
    [0, -3.24, -3.25],
)

require(engine.closed, "Exact -3.25 did not exit")
require(
    engine.exit_reason == EXIT_INITIAL_STOP,
    "Exact stop used wrong reason",
)


# ============================================================
# 4. Huge CALL winner in a few observations
# +1 -> +7 -> +15 -> +24
# Must NOT sell simply because it is strongly winning.
# ============================================================

engine, events = run(
    CALL,
    [0, 1, 7, 15, 24],
)

require(
    not engine.closed,
    "Strong CALL winner was exited prematurely",
)
require(engine.let_it_ride, "Let It Ride never armed")
require(
    engine.max_favorable == 24,
    "CALL high-water mark incorrect",
)


# ============================================================
# 5. Huge PUT winner
# SPX falling 24 points = +24 favorable for PUT.
# ============================================================

engine, events = run(
    PUT,
    [0, -1, -7, -15, -24],
)

require(
    not engine.closed,
    "Strong PUT winner was exited prematurely",
)
require(engine.let_it_ride, "PUT Let It Ride never armed")
require(
    engine.max_favorable == 24,
    "PUT high-water mark incorrect",
)


# ============================================================
# 6. CALL runner then EXACT 3-point reversal
# Peak +24 -> +21
# ============================================================

engine, events = run(
    CALL,
    [0, 5, 12, 24, 22, 21],
)

require(
    engine.closed,
    "Exact 3-point CALL reversal did not exit",
)
require(
    engine.exit_reason == EXIT_PEAK_REVERSAL,
    "CALL reversal used wrong exit reason",
)


# ============================================================
# 7. 2.99-point reversal must NOT trigger
# ============================================================

engine, events = run(
    CALL,
    [0, 5, 12, 24, 21.01],
)

require(
    not engine.closed,
    "2.99-point reversal incorrectly exited",
)


# ============================================================
# 8. Jump THROUGH reversal threshold
# Peak +20 -> next observation +14.
# Must exit immediately when +14 is observed.
# We do NOT pretend execution happened at +17.
# ============================================================

engine, events = run(
    CALL,
    [0, 6, 20, 14],
)

require(
    engine.closed,
    "Gap through peak-reversal threshold did not exit",
)
require(
    engine.exit_reason == EXIT_PEAK_REVERSAL,
    "Gap reversal used wrong exit",
)


# ============================================================
# 9. +5 reached instantly, then reverses 3 points
# 0 -> +8 -> +5
# Peak is +8, not +5.
# ============================================================

engine, events = run(
    CALL,
    [0, 8, 5],
)

require(engine.closed, "Instant winner reversal did not exit")
require(
    engine.exit_reason == EXIT_PEAK_REVERSAL,
    "Instant winner used wrong exit",
)


# ============================================================
# 10. Touch +5 then exactly 3 points backward
# +5 -> +2
# ============================================================

engine, events = run(
    CALL,
    [0, 5, 2],
)

require(
    engine.closed,
    "+5 then 3-point reversal did not exit",
)


# ============================================================
# 11. IMPORTANT UNRESOLVED AREA:
#
# +4.8 -> violent reversal.
#
# We deliberately DO NOT invent a winner-protection rule here.
# Under the CURRENT incomplete specification, Let It Ride has
# not armed yet. If price subsequently crosses -3.25, the
# initial stop is what ultimately exits.
#
# This test documents the gap instead of hiding it.
# ============================================================

engine, events = run(
    CALL,
    [0, 4.8, 2, 0.5],
)

require(
    not engine.closed,
    "Pre-+5 protection was silently invented",
)
require(
    not engine.let_it_ride,
    "Let It Ride armed before +5",
)


# ============================================================
# 12. Same pre-+5 reversal eventually crosses initial stop
# ============================================================

engine, events = run(
    CALL,
    [0, 4.8, 2, 0.5, -4],
)

require(
    engine.closed,
    "Pre-target reversal never respected initial stop",
)
require(
    engine.exit_reason == EXIT_INITIAL_STOP,
    "Pre-target loser used wrong exit",
)


# ============================================================
# 13. Mirrored PUT runner and reversal
#
# SPX path:
# 0 -> -5 -> -12 -> -20 -> -17
#
# For PUT:
# favorable path = 0 -> +5 -> +12 -> +20 -> +17
# ============================================================

engine, events = run(
    PUT,
    [0, -5, -12, -20, -17],
)

require(
    engine.closed,
    "PUT 3-point peak reversal did not exit",
)
require(
    engine.exit_reason == EXIT_PEAK_REVERSAL,
    "PUT reversal used wrong exit",
)


print()
print("=" * 72)
print("ALL MORTIFICATIO VIOLENT-MOVE ATTACK TESTS PASS")
print("CALL / PUT MIRRORING PASS")
print("INITIAL -3.25 GAP-THROUGH DETECTION PASS")
print("LARGE WINNER LET-IT-RIDE PASS")
print("3.00-POINT HIGH-WATER REVERSAL PASS")
print("REVERSAL GAP-THROUGH DETECTION PASS")
print("PRE-+5 PROTECTION REMAINS EXPLICITLY UNRESOLVED")
print("NO FILL PRICE IS FABRICATED AT A CROSSED THRESHOLD")
print("ZERO IBKR ORDERS")
print("=" * 72)
