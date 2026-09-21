from strategy_test import Trade, Direction


def check(name, direction, entry, contracts, prices,
          expected_closed, expected_remaining):

    print("\n" + "=" * 65)
    print(name)
    print("=" * 65)

    trade = Trade(direction, entry, contracts)

    for price in prices:
        # Intentionally continue sending updates even after closure.
        # The engine must ignore them safely.
        trade.update(price)

    print(
        f"RESULT | closed={trade.closed} | "
        f"remaining={trade.remaining} | "
        f"peak={trade.max_favorable:+.2f}"
    )

    assert trade.closed == expected_closed, (
        f"{name}: wrong closed state"
    )

    assert trade.remaining == expected_remaining, (
        f"{name}: expected {expected_remaining} remaining, "
        f"got {trade.remaining}"
    )

    assert 0 <= trade.remaining <= contracts, (
        f"{name}: impossible position quantity"
    )

    print("PASS")


# ------------------------------------------------------------
# 1. Price gaps straight through the -3.25 stop.
# It must still exit.
# ------------------------------------------------------------
check(
    "ATTACK 1 - GAP THROUGH INITIAL STOP",
    Direction.CALL,
    7700.00,
    25,
    [7700.00, 7699.50, 7695.00],
    True,
    0,
)


# ------------------------------------------------------------
# 2. Same test mirrored for PUTS.
# ------------------------------------------------------------
check(
    "ATTACK 2 - PUT GAP THROUGH INITIAL STOP",
    Direction.PUT,
    7700.00,
    25,
    [7700.00, 7700.50, 7705.00],
    True,
    0,
)


# ------------------------------------------------------------
# 3. Price jumps from +4 directly to +6.40.
# It crossed +5 and +6.
# Executor should simulate selling 2 at each level.
# 25 - 4 = 21 remaining.
# ------------------------------------------------------------
check(
    "ATTACK 3 - JUMP THROUGH MULTIPLE HARVEST LEVELS",
    Direction.CALL,
    7700.00,
    25,
    [7704.00, 7706.40],
    False,
    21,
)


# ------------------------------------------------------------
# 4. Exact 1-point reversal from peak.
# Peak +7.50 -> +6.50 must EXIT ALL.
# ------------------------------------------------------------
check(
    "ATTACK 4 - EXACT TRAILING BOUNDARY",
    Direction.CALL,
    7700.00,
    25,
    [
        7705.00,
        7706.00,
        7707.00,
        7707.50,
        7706.50,
    ],
    True,
    0,
)


# ------------------------------------------------------------
# 5. 0.99 reversal should NOT exit.
# Peak +7.50 -> +6.51.
# ------------------------------------------------------------
check(
    "ATTACK 5 - JUST INSIDE TRAILING STOP",
    Direction.CALL,
    7700.00,
    25,
    [
        7705.00,
        7706.00,
        7707.00,
        7707.50,
        7706.51,
    ],
    False,
    19,
)


# ------------------------------------------------------------
# 6. Only one contract.
# At +5, sell min(2, remaining), which is 1.
# Must never produce a negative position.
# ------------------------------------------------------------
check(
    "ATTACK 6 - ONE CONTRACT",
    Direction.CALL,
    7700.00,
    1,
    [7705.00],
    True,
    0,
)


# ------------------------------------------------------------
# 7. Repeated identical ticks.
# +5 repeated several times must NOT repeatedly sell.
# Only first +5 crossing sells 2.
# ------------------------------------------------------------
check(
    "ATTACK 7 - DUPLICATE PRICE UPDATES",
    Direction.CALL,
    7700.00,
    25,
    [
        7705.00,
        7705.00,
        7705.00,
        7705.00,
    ],
    False,
    23,
)


# ------------------------------------------------------------
# 8. After position closes, send more crazy prices.
# Nothing is allowed to resurrect the trade or sell again.
# ------------------------------------------------------------
check(
    "ATTACK 8 - UPDATES AFTER POSITION CLOSED",
    Direction.CALL,
    7700.00,
    25,
    [
        7696.75,  # closes position
        7710.00,
        7690.00,
        7720.00,
        7600.00,
    ],
    True,
    0,
)


# ------------------------------------------------------------
# 9. Huge favorable jump.
# Crosses +5 through +12.
# That's 8 harvest levels:
# 5,6,7,8,9,10,11,12
# 8 x 2 = 16 sold
# 25 - 16 = 9 remaining.
# ------------------------------------------------------------
check(
    "ATTACK 9 - HUGE FAVORABLE JUMP",
    Direction.CALL,
    7700.00,
    25,
    [7712.40],
    False,
    9,
)


# ------------------------------------------------------------
# 10. Huge jump followed by 1-point reversal.
# Remaining contracts must all disappear.
# ------------------------------------------------------------
check(
    "ATTACK 10 - HUGE JUMP THEN TRAILING EXIT",
    Direction.CALL,
    7700.00,
    25,
    [
        7712.40,
        7711.40,
    ],
    True,
    0,
)


# ------------------------------------------------------------
# 11. Mirror a complicated harvest/trailing sequence with PUTS.
# ------------------------------------------------------------
check(
    "ATTACK 11 - PUT HARVEST AND TRAIL",
    Direction.PUT,
    7700.00,
    25,
    [
        7695.00,  # +5
        7694.00,  # +6
        7693.00,  # +7
        7692.40,  # peak +7.60
        7693.40,  # exactly 1-point reversal
    ],
    True,
    0,
)


print("\n" + "=" * 65)
print("ALL ATTACK TESTS PASS")
print("SIMULATION ONLY - ZERO IBKR ORDERS")
print("=" * 65)
