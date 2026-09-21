from dataclasses import dataclass
from enum import Enum, auto
import math


# ============================================================
# EXECUTOR MARKET-DATA SAFETY
# ============================================================

MAX_AGE_SECONDS = 1.00


class FeedType(Enum):
    LIVE = auto()
    FROZEN = auto()
    DELAYED = auto()
    DELAYED_FROZEN = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class Quote:
    feed_type: FeedType
    age_seconds: float
    bid: float = None
    ask: float = None
    last: float = None


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str


def valid_number(x):
    return (
        x is not None
        and isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
        and x > 0
    )


def market_data_gate(
    spx: Quote,
    option: Quote,
) -> GateResult:

    # --------------------------------------------------------
    # 1. BOTH FEEDS MUST EXPLICITLY BE LIVE
    # --------------------------------------------------------

    if spx.feed_type != FeedType.LIVE:
        return GateResult(
            False,
            "SPX_DATA_NOT_LIVE",
        )

    if option.feed_type != FeedType.LIVE:
        return GateResult(
            False,
            "OPTION_DATA_NOT_LIVE",
        )

    # --------------------------------------------------------
    # 2. BOTH FEEDS MUST BE FRESH
    # --------------------------------------------------------

    if (
        not valid_age(spx.age_seconds)
    ):
        return GateResult(False, "SPX_DATA_STALE")

    if (
        not valid_age(option.age_seconds)
    ):
        return GateResult(False, "OPTION_DATA_STALE")

    # --------------------------------------------------------
    # 3. SPX NEEDS A VALID PRICE
    # --------------------------------------------------------

    if not valid_number(spx.last):
        return GateResult(
            False,
            "SPX_PRICE_INVALID",
        )

    # --------------------------------------------------------
    # 4. OPTION NEEDS A REAL TWO-SIDED MARKET
    # --------------------------------------------------------

    if not valid_number(option.bid):
        return GateResult(
            False,
            "OPTION_BID_INVALID",
        )

    if not valid_number(option.ask):
        return GateResult(
            False,
            "OPTION_ASK_INVALID",
        )

    if option.ask < option.bid:
        return GateResult(
            False,
            "OPTION_MARKET_CROSSED",
        )

    # --------------------------------------------------------
    # ALL SAFETY CONDITIONS PASSED
    # --------------------------------------------------------

    return GateResult(
        True,
        "LIVE_FRESH_MARKET_DATA_CONFIRMED",
    )


def valid_age(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= MAX_AGE_SECONDS
    )


def live_spx(age=0.10, last=7700.00):
    return Quote(
        FeedType.LIVE,
        age,
        last=last,
    )


def live_option(
    age=0.10,
    bid=9.90,
    ask=10.00,
):
    return Quote(
        FeedType.LIVE,
        age,
        bid=bid,
        ask=ask,
    )


def expect_block(spx, option, reason):
    result = market_data_gate(spx, option)

    assert result.allowed is False
    assert result.reason == reason


print()
print("========================================")
print("MARKET-DATA SAFETY ATTACK TESTS")
print("========================================")


# 1. Perfect live data.
r = market_data_gate(
    live_spx(),
    live_option(),
)

assert r.allowed is True
print("1. LIVE + FRESH DATA: PASS")


# 2. Delayed SPX must block.
expect_block(
    Quote(
        FeedType.DELAYED,
        0.10,
        last=7700,
    ),
    live_option(),
    "SPX_DATA_NOT_LIVE",
)
print("2. DELAYED SPX BLOCKED: PASS")


# 3. Frozen SPX must block.
expect_block(
    Quote(
        FeedType.FROZEN,
        0.10,
        last=7700,
    ),
    live_option(),
    "SPX_DATA_NOT_LIVE",
)
print("3. FROZEN SPX BLOCKED: PASS")


# 4. Delayed option must block.
expect_block(
    live_spx(),
    Quote(
        FeedType.DELAYED,
        0.10,
        bid=9.90,
        ask=10.00,
    ),
    "OPTION_DATA_NOT_LIVE",
)
print("4. DELAYED OPTION BLOCKED: PASS")


# 5. Unknown market-data type must block.
expect_block(
    Quote(
        FeedType.UNKNOWN,
        0.10,
        last=7700,
    ),
    live_option(),
    "SPX_DATA_NOT_LIVE",
)
print("5. UNKNOWN FEED BLOCKED: PASS")


# 6. SPX older than one second blocks.
expect_block(
    live_spx(age=1.01),
    live_option(),
    "SPX_DATA_STALE",
)
print("6. STALE SPX BLOCKED: PASS")


# 7. Option older than one second blocks.
expect_block(
    live_spx(),
    live_option(age=1.01),
    "OPTION_DATA_STALE",
)
print("7. STALE OPTION BLOCKED: PASS")


# 8. Exactly one second is still accepted.
r = market_data_gate(
    live_spx(age=1.00),
    live_option(age=1.00),
)

assert r.allowed is True
print("8. EXACT FRESHNESS BOUNDARY: PASS")


# 9. Negative age is impossible/unsafe.
expect_block(
    live_spx(age=-0.01),
    live_option(),
    "SPX_DATA_STALE",
)
print("9. IMPOSSIBLE TIMESTAMP BLOCKED: PASS")


# 10. Missing SPX price blocks.
expect_block(
    Quote(
        FeedType.LIVE,
        0.10,
        last=None,
    ),
    live_option(),
    "SPX_PRICE_INVALID",
)
print("10. MISSING SPX PRICE BLOCKED: PASS")


# 11. Zero SPX price blocks.
expect_block(
    live_spx(last=0),
    live_option(),
    "SPX_PRICE_INVALID",
)
print("11. INVALID SPX PRICE BLOCKED: PASS")


# 12. Missing option bid blocks.
expect_block(
    live_spx(),
    live_option(bid=None),
    "OPTION_BID_INVALID",
)
print("12. MISSING OPTION BID BLOCKED: PASS")


# 13. Missing option ask blocks.
expect_block(
    live_spx(),
    live_option(ask=None),
    "OPTION_ASK_INVALID",
)
print("13. MISSING OPTION ASK BLOCKED: PASS")


# 14. Zero ask blocks.
expect_block(
    live_spx(),
    live_option(ask=0),
    "OPTION_ASK_INVALID",
)
print("14. ZERO OPTION ASK BLOCKED: PASS")


# 15. Crossed market blocks.
expect_block(
    live_spx(),
    live_option(
        bid=10.10,
        ask=10.00,
    ),
    "OPTION_MARKET_CROSSED",
)
print("15. CROSSED OPTION MARKET BLOCKED: PASS")

for bad in (float("nan"), float("inf"), -float("inf"), None):
    expect_block(live_spx(age=bad), live_option(), "SPX_DATA_STALE")
    expect_block(live_spx(), live_option(age=bad), "OPTION_DATA_STALE")
    expect_block(live_spx(last=bad), live_option(), "SPX_PRICE_INVALID")
    expect_block(live_spx(), live_option(bid=bad), "OPTION_BID_INVALID")
    expect_block(live_spx(), live_option(ask=bad), "OPTION_ASK_INVALID")
print("16. NON-FINITE OR MISSING QUOTES AND AGES BLOCKED: PASS")


print()
print("========================================")
print("ALL MARKET-DATA SAFETY TESTS PASS")
print("DELAYED / FROZEN / STALE DATA BLOCKED")
print("ZERO IBKR ORDERS")
print("========================================")
print()
