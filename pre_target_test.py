from dataclasses import dataclass
from typing import List, Optional


INITIAL_STOP = -3.25
TARGET = 5.00


@dataclass
class Result:
    rule: str
    peak: float
    exit_move: Optional[float]
    reason: str


def simulate(
    moves: List[float],
    arm_at: float,
    protected_floor: float,
) -> Result:
    """
    EXPERIMENT ONLY.

    - Until protection arms, hard stop remains -3.25.
    - Once favorable movement reaches arm_at,
      the minimum acceptable favorable move becomes protected_floor.
    - +5 hands control to Let-It-Ride.
    """

    peak = 0.0
    protection_armed = False

    for move in moves:
        peak = max(peak, move)

        # Reached +5: this experimental rule did its job.
        if move >= TARGET:
            return Result(
                rule=f"arm {arm_at:.1f} / floor {protected_floor:+.1f}",
                peak=peak,
                exit_move=None,
                reason="REACHED +5 -> LET IT RIDE",
            )

        # Initial losing stop
        if not protection_armed and move <= INITIAL_STOP:
            return Result(
                rule=f"arm {arm_at:.1f} / floor {protected_floor:+.1f}",
                peak=peak,
                exit_move=move,
                reason="INITIAL -3.25 STOP",
            )

        # Arm winner protection
        if peak >= arm_at:
            protection_armed = True

        # Once armed, don't allow the protected floor to be crossed
        if protection_armed and move <= protected_floor:
            return Result(
                rule=f"arm {arm_at:.1f} / floor {protected_floor:+.1f}",
                peak=peak,
                exit_move=move,
                reason="PRE-TARGET PROTECTION",
            )

    return Result(
        rule=f"arm {arm_at:.1f} / floor {protected_floor:+.1f}",
        peak=peak,
        exit_move=None,
        reason="STILL OPEN",
    )


RULES = [
    (2.0, 0.0),
    (2.5, 0.0),
    (3.0, 0.0),
    (3.0, 0.5),
    (3.5, 0.5),
    (3.5, 1.0),
    (4.0, 1.0),
    (4.0, 1.5),
]


CASES = {
    "CASE 1 - IMMEDIATE LOSER": [
        0.0, -1.0, -2.0, -3.0, -3.30
    ],

    "CASE 2 - +3 THEN COMPLETE FAILURE": [
        0.0, 1.0, 2.0, 3.0, 2.0, 1.0, 0.0, -1.0
    ],

    "CASE 3 - +4.8 THEN COMPLETE REVERSAL": [
        0.0, 1.0, 2.5, 4.0, 4.8, 4.0, 3.0, 2.0, 1.0, 0.0
    ],

    "CASE 4 - +4.8 PULLBACK THEN +5": [
        0.0, 1.0, 3.0, 4.0, 4.8, 4.2, 3.8, 4.4, 5.0
    ],

    "CASE 5 - +4.8 DEEPER PULLBACK THEN RUNNER": [
        0.0, 2.0, 3.5, 4.8, 3.5, 3.0, 4.0, 4.7, 5.1, 6.0
    ],

    "CASE 6 - +2.5 THEN NORMAL NOISE THEN +5": [
        0.0, 1.5, 2.5, 1.5, 0.8, 1.8, 3.0, 4.0, 5.0
    ],
}


for case_name, path in CASES.items():
    print("\n" + "=" * 84)
    print(case_name)
    print("PATH:", " -> ".join(f"{x:+.2f}" for x in path))
    print("=" * 84)

    for arm, floor in RULES:
        r = simulate(path, arm, floor)

        exit_text = (
            f"{r.exit_move:+.2f}"
            if r.exit_move is not None
            else "-----"
        )

        print(
            f"{r.rule:24} | "
            f"peak={r.peak:+5.2f} | "
            f"exit={exit_text:>5} | "
            f"{r.reason}"
        )


print("\n" + "=" * 84)
print("PRE-TARGET COMPARISON COMPLETE")
print("NO PRE-TARGET MODEL HAS BEEN SELECTED")
print("SIMULATION ONLY - ZERO IBKR ORDERS")
print("=" * 84)
