import math
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

MAX_CONTRACTS = 25
MULTIPLIER = 100


def valid_money(x):
    return (
        isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
        and x >= 0
    )


def valid_ask(x):
    return (
        isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
        and x > 0
    )


def calculate_qty(usable_funds, ask):
    if not valid_money(usable_funds):
        return 0, "INVALID_USABLE_FUNDS"

    if not valid_ask(ask):
        return 0, "INVALID_ASK"

    try:
        funds_decimal = Decimal(str(usable_funds))
        ask_decimal = Decimal(str(ask))
        contract_cost = ask_decimal * Decimal(MULTIPLIER)

        qty = int(
            (funds_decimal / contract_cost).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
    except (InvalidOperation, ValueError, OverflowError):
        return 0, "INVALID_SIZING_INPUT"

    qty = min(qty, MAX_CONTRACTS)

    if qty < 1:
        return 0, "CANNOT_AFFORD_ONE_CONTRACT"

    return qty, "OK"


def revalidate_before_chunk(
    usable_funds,
    current_ask,
    remaining_qty,
):
    if (
        not isinstance(remaining_qty, int)
        or isinstance(remaining_qty, bool)
        or remaining_qty <= 0
    ):
        return 0, "INVALID_REMAINING_QTY"

    affordable, reason = calculate_qty(
        usable_funds,
        current_ask,
    )

    if affordable == 0:
        return 0, reason

    # CRITICAL:
    # Revalidation may REDUCE the remaining order,
    # but it may never increase beyond what was
    # already authorized.
    safe_qty = min(
        affordable,
        remaining_qty,
    )

    return safe_qty, "OK"


def check(
    number,
    name,
    funds,
    ask,
    expected_qty,
    expected_reason="OK",
):
    qty, reason = calculate_qty(funds, ask)

    assert qty == expected_qty, (
        f"{name}: expected qty {expected_qty}, got {qty}"
    )

    assert reason == expected_reason, (
        f"{name}: expected {expected_reason}, got {reason}"
    )

    print(f"{number}. {name}: PASS")


print()
print("========================================")
print("POSITION-SIZING SAFETY ATTACK TESTS")
print("========================================")


# $1,000 / $988 contract = 1.
check(
    1,
    "$1,000 WITH $9.88 ASK",
    1000,
    9.88,
    1,
)


# Exactly enough for one.
check(
    2,
    "EXACT ONE-CONTRACT BOUNDARY",
    988,
    9.88,
    1,
)


# One cent short.
check(
    3,
    "ONE CENT BELOW AFFORDABILITY",
    987.99,
    9.88,
    0,
    "CANNOT_AFFORD_ONE_CONTRACT",
)


# Exactly enough for 10.
check(
    4,
    "EXACT TEN-CONTRACT BOUNDARY",
    9880,
    9.88,
    10,
)


# 24 contracts.
check(
    5,
    "TWENTY-FOUR CONTRACTS",
    23712,
    9.88,
    24,
)


# Exactly 25.
check(
    6,
    "EXACT 25-CONTRACT CAP",
    24700,
    9.88,
    25,
)


# Enough money for far more than 25.
check(
    7,
    "HARD 25-CONTRACT CAP",
    1000000,
    9.88,
    25,
)


# Zero buying power.
check(
    8,
    "ZERO USABLE FUNDS",
    0,
    9.88,
    0,
    "CANNOT_AFFORD_ONE_CONTRACT",
)


# Negative buying power is invalid.
check(
    9,
    "NEGATIVE USABLE FUNDS",
    -1,
    9.88,
    0,
    "INVALID_USABLE_FUNDS",
)


# Invalid ask.
check(
    10,
    "ZERO ASK",
    10000,
    0,
    0,
    "INVALID_ASK",
)


# Missing ask.
check(
    11,
    "MISSING ASK",
    10000,
    None,
    0,
    "INVALID_ASK",
)


# NaN funds.
check(
    12,
    "NaN USABLE FUNDS",
    float("nan"),
    9.88,
    0,
    "INVALID_USABLE_FUNDS",
)


# Infinite funds must not become 25.
check(
    13,
    "INFINITE USABLE FUNDS",
    float("inf"),
    9.88,
    0,
    "INVALID_USABLE_FUNDS",
)


# NaN ask.
check(
    14,
    "NaN ASK",
    10000,
    float("nan"),
    0,
    "INVALID_ASK",
)


# ------------------------------------------------------------
# PRICE-JUMP REVALIDATION
# ------------------------------------------------------------

# Initially authorized for 10 contracts at $10 ask.
initial_qty, reason = calculate_qty(
    10000,
    10.00,
)

assert initial_qty == 10
assert reason == "OK"

# Before execution, ask jumps to $12.
# Only 8 are now affordable.
safe_qty, reason = revalidate_before_chunk(
    10000,
    12.00,
    initial_qty,
)

assert safe_qty == 8
assert reason == "OK"

print("15. ASK JUMP REDUCES QUANTITY: PASS")


# ------------------------------------------------------------
# PRICE DROP MUST NOT INCREASE AUTHORIZED QUANTITY
# ------------------------------------------------------------

initial_qty, reason = calculate_qty(
    5000,
    10.00,
)

assert initial_qty == 5

# Ask drops dramatically.
# We could mathematically afford more now,
# but this request was authorized for only 5.
safe_qty, reason = revalidate_before_chunk(
    5000,
    5.00,
    initial_qty,
)

assert safe_qty == 5
assert reason == "OK"

print("16. PRICE DROP CANNOT INCREASE SIZE: PASS")


# ------------------------------------------------------------
# FUNDS DROP BEFORE EXECUTION
# ------------------------------------------------------------

initial_qty, _ = calculate_qty(
    10000,
    10.00,
)

assert initial_qty == 10

safe_qty, reason = revalidate_before_chunk(
    4500,
    10.00,
    initial_qty,
)

assert safe_qty == 4
assert reason == "OK"

print("17. BUYING-POWER DROP REDUCES SIZE: PASS")


# ------------------------------------------------------------
# FUNDS DROP BELOW ONE CONTRACT
# ------------------------------------------------------------

safe_qty, reason = revalidate_before_chunk(
    900,
    10.00,
    10,
)

assert safe_qty == 0
assert reason == "CANNOT_AFFORD_ONE_CONTRACT"

print("18. UNAFFORDABLE REVALIDATION BLOCKED: PASS")


# ------------------------------------------------------------
# REMAINING QUANTITY IS A HARD CEILING
# ------------------------------------------------------------

safe_qty, reason = revalidate_before_chunk(
    100000,
    1.00,
    3,
)

assert safe_qty == 3
assert reason == "OK"

print("19. REMAINING QUANTITY HARD CEILING: PASS")


# ------------------------------------------------------------
# INVALID REMAINING QUANTITY
# ------------------------------------------------------------

safe_qty, reason = revalidate_before_chunk(
    10000,
    10.00,
    0,
)

assert safe_qty == 0
assert reason == "INVALID_REMAINING_QTY"

print("20. INVALID REMAINING QUANTITY BLOCKED: PASS")


print()
print("========================================")
print("ALL POSITION-SIZING ATTACK TESTS PASS")
print("25-CONTRACT HARD CAP ENFORCED")
print("ASK/FUNDS CHANGES CAN ONLY REDUCE SIZE")
print("SIMULATION ONLY")
print("ZERO IBKR ORDERS")
print("========================================")
print()
