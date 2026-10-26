"""Shared read-only strike ordering. No sizing, subscriptions, or broker calls."""
from decimal import Decimal


def nearby_listed_strikes(strikes, spx_price, count=3):
    return tuple(sorted(set(strikes), key=lambda strike: (abs(strike-spx_price), strike))[:count])


def atm_delta_rank(delta, strike, spx_price, con_id):
    return (abs(abs(Decimal(str(delta)))-Decimal('0.48')),
            abs(strike-spx_price), strike, con_id)
