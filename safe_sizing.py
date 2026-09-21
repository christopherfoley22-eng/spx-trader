"""Pure, fail-closed simulated option affordability calculation."""

from decimal import Decimal, InvalidOperation

MAX_CONTRACTS = 25
OPTION_MULTIPLIER = Decimal("100")


def max_affordable_contracts(usable_funds, ask):
    if isinstance(usable_funds, bool) or isinstance(ask, bool):
        return 0
    try:
        funds = Decimal(str(usable_funds))
        price = Decimal(str(ask))
    except (InvalidOperation, TypeError, ValueError):
        return 0
    if not funds.is_finite() or not price.is_finite() or funds <= 0 or price <= 0:
        return 0
    cost = price * OPTION_MULTIPLIER
    return min(int(funds // cost), MAX_CONTRACTS)
