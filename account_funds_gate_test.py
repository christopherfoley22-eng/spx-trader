from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math


MAX_AGE_SECONDS = 1.0


@dataclass
class AccountSnapshot:
    currency: str
    age_seconds: float

    available_funds: object
    buying_power: object
    excess_liquidity: object
    total_cash_value: object
    settled_cash: object


@dataclass
class FundsDecision:
    allowed: bool
    usable_funds: object
    reason: str


def money(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None

    if not d.is_finite():
        return None

    return d


def valid_age(value):
    if isinstance(value, bool):
        return False

    try:
        x = float(value)
    except (TypeError, ValueError):
        return False

    return (
        math.isfinite(x)
        and x >= 0
        and x <= MAX_AGE_SECONDS
    )


def determine_usable_funds(snapshot):
    # --------------------------------------------------------
    # SNAPSHOT INTEGRITY
    # --------------------------------------------------------

    if snapshot.currency != "USD":
        return FundsDecision(
            False,
            None,
            "CURRENCY_NOT_USD",
        )

    if not valid_age(snapshot.age_seconds):
        return FundsDecision(
            False,
            None,
            "STALE_OR_INVALID_ACCOUNT_DATA",
        )

    available = money(snapshot.available_funds)
    buying = money(snapshot.buying_power)
    excess = money(snapshot.excess_liquidity)
    cash = money(snapshot.total_cash_value)
    settled = money(snapshot.settled_cash)

    required = {
        "AvailableFunds": available,
        "BuyingPower": buying,
        "ExcessLiquidity": excess,
        "TotalCashValue": cash,
        "SettledCash": settled,
    }

    for name, value in required.items():
        if value is None:
            return FundsDecision(
                False,
                None,
                f"INVALID_OR_MISSING_{name}",
            )

        if value < 0:
            return FundsDecision(
                False,
                None,
                f"NEGATIVE_{name}",
            )

    # --------------------------------------------------------
    # CONSERVATIVE SAFETY RULE
    #
    # BuyingPower is observed and validated, but NEVER used
    # by itself to increase position size.
    #
    # Until funded-account behavior is verified, Executor uses
    # the lowest of the cash/availability safety fields.
    # --------------------------------------------------------

    usable = min(
        available,
        excess,
        cash,
        settled,
    )

    if usable <= 0:
        return FundsDecision(
            False,
            None,
            "NO_USABLE_FUNDS",
        )

    return FundsDecision(
        True,
        usable,
        "OK",
    )


def expect(condition, message):
    assert condition, message


def base_snapshot():
    return AccountSnapshot(
        currency="USD",
        age_seconds=0.20,
        available_funds="10000.00",
        buying_power="10000.00",
        excess_liquidity="10000.00",
        total_cash_value="10000.00",
        settled_cash="10000.00",
    )


print()
print("========================================")
print("ACCOUNT-FUNDS SAFETY ATTACK TESTS")
print("========================================")


# 1
s = base_snapshot()
d = determine_usable_funds(s)

expect(d.allowed, d.reason)
expect(d.usable_funds == Decimal("10000.00"), d)

print("1. VALID ACCOUNT SNAPSHOT ACCEPTED: PASS")


# 2
s = base_snapshot()
s.buying_power = "1000000.00"

d = determine_usable_funds(s)

expect(d.allowed, d.reason)
expect(
    d.usable_funds == Decimal("10000.00"),
    "BuyingPower improperly increased usable funds",
)

print("2. HUGE BUYING POWER CANNOT INFLATE SIZE: PASS")


# 3
s = base_snapshot()
s.available_funds = "8000.00"

d = determine_usable_funds(s)

expect(d.allowed, d.reason)
expect(d.usable_funds == Decimal("8000.00"), d)

print("3. LOWER AVAILABLE FUNDS WINS: PASS")


# 4
s = base_snapshot()
s.excess_liquidity = "7000.00"

d = determine_usable_funds(s)

expect(d.allowed, d.reason)
expect(d.usable_funds == Decimal("7000.00"), d)

print("4. LOWER EXCESS LIQUIDITY WINS: PASS")


# 5
s = base_snapshot()
s.total_cash_value = "6000.00"

d = determine_usable_funds(s)

expect(d.allowed, d.reason)
expect(d.usable_funds == Decimal("6000.00"), d)

print("5. LOWER TOTAL CASH VALUE WINS: PASS")


# 6
s = base_snapshot()
s.settled_cash = "5000.00"

d = determine_usable_funds(s)

expect(d.allowed, d.reason)
expect(d.usable_funds == Decimal("5000.00"), d)

print("6. LOWER SETTLED CASH WINS: PASS")


# 7
s = base_snapshot()
s.currency = "EUR"

d = determine_usable_funds(s)

expect(not d.allowed, "Non-USD snapshot accepted")
expect(d.reason == "CURRENCY_NOT_USD", d.reason)

print("7. NON-USD SNAPSHOT BLOCKED: PASS")


# 8
s = base_snapshot()
s.age_seconds = 1.01

d = determine_usable_funds(s)

expect(not d.allowed, "Stale snapshot accepted")
expect(
    d.reason == "STALE_OR_INVALID_ACCOUNT_DATA",
    d.reason,
)

print("8. STALE ACCOUNT SNAPSHOT BLOCKED: PASS")


# 9
s = base_snapshot()
s.age_seconds = 1.00

d = determine_usable_funds(s)

expect(d.allowed, d.reason)

print("9. EXACT FRESHNESS BOUNDARY ACCEPTED: PASS")


# 10
s = base_snapshot()
s.age_seconds = -0.01

d = determine_usable_funds(s)

expect(not d.allowed, "Negative age accepted")

print("10. NEGATIVE SNAPSHOT AGE BLOCKED: PASS")


# 11
s = base_snapshot()
s.available_funds = None

d = determine_usable_funds(s)

expect(not d.allowed, "Missing AvailableFunds accepted")
expect(
    d.reason == "INVALID_OR_MISSING_AvailableFunds",
    d.reason,
)

print("11. MISSING AVAILABLE FUNDS BLOCKED: PASS")


# 12
s = base_snapshot()
s.settled_cash = None

d = determine_usable_funds(s)

expect(not d.allowed, "Missing SettledCash accepted")

print("12. MISSING SETTLED CASH BLOCKED: PASS")


# 13
s = base_snapshot()
s.buying_power = None

d = determine_usable_funds(s)

expect(not d.allowed, "Missing BuyingPower accepted")

print("13. MISSING BUYING POWER BLOCKED: PASS")


# 14
s = base_snapshot()
s.available_funds = "NaN"

d = determine_usable_funds(s)

expect(not d.allowed, "NaN accepted")

print("14. NaN ACCOUNT VALUE BLOCKED: PASS")


# 15
s = base_snapshot()
s.total_cash_value = "Infinity"

d = determine_usable_funds(s)

expect(not d.allowed, "Infinity accepted")

print("15. INFINITE ACCOUNT VALUE BLOCKED: PASS")


# 16
s = base_snapshot()
s.available_funds = "-1.00"

d = determine_usable_funds(s)

expect(not d.allowed, "Negative AvailableFunds accepted")

print("16. NEGATIVE AVAILABLE FUNDS BLOCKED: PASS")


# 17
s = base_snapshot()
s.excess_liquidity = "-1.00"

d = determine_usable_funds(s)

expect(not d.allowed, "Negative ExcessLiquidity accepted")

print("17. NEGATIVE EXCESS LIQUIDITY BLOCKED: PASS")


# 18
s = base_snapshot()
s.settled_cash = "0.00"

d = determine_usable_funds(s)

expect(not d.allowed, "Zero conservative funds accepted")
expect(d.reason == "NO_USABLE_FUNDS", d.reason)

print("18. ZERO CONSERVATIVE FUNDS BLOCK ENTRY: PASS")


# 19
# Mirrors the current real account values from the probe.
s = AccountSnapshot(
    currency="USD",
    age_seconds=0.10,
    available_funds="6.39",
    buying_power="6.39",
    excess_liquidity="6.39",
    total_cash_value="6.39",
    settled_cash="6.39",
)

d = determine_usable_funds(s)

expect(d.allowed, d.reason)
expect(d.usable_funds == Decimal("6.39"), d)

print("19. CURRENT $6.39 SNAPSHOT PARSES SAFELY: PASS")


# 20
# Funds gate can accept the snapshot while the later option
# sizing gate still correctly determines that $6.39 cannot
# buy an SPX contract. These are separate responsibilities.
s = base_snapshot()
s.available_funds = "6.39"
s.excess_liquidity = "6.39"
s.total_cash_value = "6.39"
s.settled_cash = "6.39"
s.buying_power = "500000.00"

d = determine_usable_funds(s)

expect(d.allowed, d.reason)
expect(
    d.usable_funds == Decimal("6.39"),
    "BuyingPower leaked into usable funds",
)

print("20. LEVERAGED BUYING POWER CANNOT OVERRIDE $6.39 CASH: PASS")


# 21
# Decimal precision must survive cents exactly.
s = base_snapshot()
s.available_funds = "988.00"
s.excess_liquidity = "988.00"
s.total_cash_value = "988.00"
s.settled_cash = "988.00"

d = determine_usable_funds(s)

expect(d.allowed, d.reason)
expect(
    d.usable_funds == Decimal("988.00"),
    "Money precision changed",
)

print("21. EXACT DECIMAL MONEY BOUNDARY PRESERVED: PASS")


# 22
s = base_snapshot()
s.age_seconds = float("nan")

d = determine_usable_funds(s)

expect(not d.allowed, "NaN age accepted")

print("22. NaN SNAPSHOT AGE BLOCKED: PASS")


# 23
s = base_snapshot()
s.age_seconds = float("inf")

d = determine_usable_funds(s)

expect(not d.allowed, "Infinite age accepted")

print("23. INFINITE SNAPSHOT AGE BLOCKED: PASS")


print()
print("========================================")
print("ALL ACCOUNT-FUNDS ATTACK TESTS PASS")
print("BUYING POWER CANNOT INFLATE POSITION SIZE")
print("MISSING / INVALID / STALE DATA FAILS CLOSED")
print("DECIMAL MONEY ARITHMETIC PRESERVED")
print("PROVISIONAL CONSERVATIVE FUNDS RULE VERIFIED")
print("SIMULATION ONLY")
print("ZERO IBKR ORDERS")
print("========================================")
print()
