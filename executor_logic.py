from dataclasses import dataclass
from typing import List, Optional
import math


MAX_CONTRACTS = 25
OPTION_MULTIPLIER = 100


@dataclass
class OptionCandidate:
    strike: float
    ask: float


@dataclass
class Selection:
    strike: float
    ask: float
    contracts: int
    total_cost: float
    distance_from_atm: float


def max_affordable_contracts(
    available_funds: float,
    ask: float,
) -> int:
    """Whole contracts only, maximum 25."""
    if available_funds <= 0 or ask <= 0:
        return 0

    contract_cost = ask * OPTION_MULTIPLIER
    qty = math.floor(available_funds / contract_cost)

    return min(qty, MAX_CONTRACTS)


def select_contract(
    spx_price: float,
    available_funds: float,
    candidates: List[OptionCandidate],
) -> Optional[Selection]:
    """
    Rule:
      1. Prefer the strike closest to current SPX.
      2. It must be affordable for at least one contract.
      3. Never intentionally move farther from ATM unless
         closer strikes cannot be afforded.
      4. Never buy more than 25 contracts.
    """

    valid = [
        c for c in candidates
        if c.ask > 0
    ]

    # ATM proximity is the primary priority.
    # Strike is only a deterministic tie-breaker.
    valid.sort(key=lambda c: (abs(c.strike - spx_price), c.strike))

    for option in valid:
        qty = max_affordable_contracts(
            available_funds,
            option.ask,
        )

        if qty >= 1:
            return Selection(
                strike=option.strike,
                ask=option.ask,
                contracts=qty,
                total_cost=qty * option.ask * OPTION_MULTIPLIER,
                distance_from_atm=abs(option.strike - spx_price),
            )

    return None


def run_test(
    name: str,
    spx: float,
    funds: float,
    options: List[OptionCandidate],
    expected_strike: Optional[float],
    expected_qty: int,
):
    result = select_contract(spx, funds, options)

    print(f"\n--- {name} ---")
    print(f"SPX: ${spx:,.2f}")
    print(f"Available funds: ${funds:,.2f}")

    if result is None:
        print("Selection: NONE")
        assert expected_strike is None
        assert expected_qty == 0
        return

    print(f"Selected strike: {result.strike}")
    print(f"Ask: ${result.ask:.2f}")
    print(f"Contracts: {result.contracts}")
    print(f"Estimated cost: ${result.total_cost:,.2f}")
    print(f"Distance from ATM: {result.distance_from_atm:.2f} SPX points")

    assert result.strike == expected_strike
    assert result.contracts == expected_qty


if __name__ == "__main__":

    # Closest strike wins.
    run_test(
        "Normal ATM selection",
        7701.20,
        5000,
        [
            OptionCandidate(7695, 8.00),
            OptionCandidate(7700, 10.00),
            OptionCandidate(7705, 7.00),
        ],
        7700,
        5,
    )

    # Closest strike is too expensive.
    # Executor must find the closest affordable alternative.
    run_test(
        "ATM unavailable because of funds",
        7701.20,
        900,
        [
            OptionCandidate(7700, 10.00),  # $1,000 -> can't buy
            OptionCandidate(7705, 8.50),   # $850 -> can buy
            OptionCandidate(7695, 12.00),
        ],
        7705,
        1,
    )

    # Hard cap at 25 contracts.
    run_test(
        "25 contract hard cap",
        7700.10,
        100000,
        [
            OptionCandidate(7700, 10.00),
        ],
        7700,
        25,
    )

    # Nothing is affordable.
    run_test(
        "No affordable contract",
        7700.00,
        500,
        [
            OptionCandidate(7700, 10.00),
            OptionCandidate(7705, 8.00),
            OptionCandidate(7695, 12.00),
        ],
        None,
        0,
    )

    print("\n================================")
    print("ALL ATM/AFFORDABILITY TESTS PASS")
    print("NO ORDERS WERE SUBMITTED")
    print("================================")
