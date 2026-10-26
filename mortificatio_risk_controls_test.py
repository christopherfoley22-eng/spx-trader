"""Synthetic offline tests for MORTIFICATIO risk preflight."""
from decimal import Decimal
from mortificatio_risk_controls import (RiskState, ResearchTimeSettings,
    entry_preflight, DEFAULT_DAILY_LOSS_LIMIT, EXISTING_DAILY_TRADE_LIMIT,
    EARLIER_PLANNED_DAILY_TRADE_LIMIT)

base = RiskState("SINGLE_ACCOUNT", 0, False, 0, Decimal("0"))
assert DEFAULT_DAILY_LOSS_LIMIT is None
assert entry_preflight(base, 25, "SINGLE_ACCOUNT").reason == "DAILY_LOSS_UNCERTAIN"
assert entry_preflight(base, 25, "SINGLE_ACCOUNT", Decimal("500")).allowed is True
assert entry_preflight(base, 26, "SINGLE_ACCOUNT").reason == "QUANTITY_LIMIT"
assert entry_preflight(RiskState("SINGLE_ACCOUNT", 1, False, 0, 0), 1, "SINGLE_ACCOUNT").reason == "ACTIVE_POSITION"
assert entry_preflight(RiskState("SINGLE_ACCOUNT", 0, True, 0, 0), 1, "SINGLE_ACCOUNT").reason == "EXIT_UNRESOLVED"
assert entry_preflight(RiskState("SINGLE_ACCOUNT", 0, False, 2, 0), 1, "SINGLE_ACCOUNT").reason == "DAILY_TRADE_LIMIT"
assert entry_preflight(RiskState("SINGLE_ACCOUNT", 0, False, 0, -500), 1, "SINGLE_ACCOUNT", Decimal("500")).reason == "DAILY_DOLLAR_LOSS_LIMIT"
assert entry_preflight(RiskState("SINGLE_ACCOUNT", 0, False, 0, -499.99), 1, "SINGLE_ACCOUNT", Decimal("500")).allowed is True
assert entry_preflight(RiskState("SINGLE_ACCOUNT", 0, False, 0, "NaN"), 1, "SINGLE_ACCOUNT", Decimal("500")).reason == "DAILY_LOSS_UNCERTAIN"
assert entry_preflight(RiskState("SINGLE_ACCOUNT", 0, False, 0, 0, False), 1, "SINGLE_ACCOUNT").reason == "RECOVERY_REQUIRED_INCONSISTENT_STATE"
assert entry_preflight(RiskState("OTHER", 0, False, 0, 0), 1, "SINGLE_ACCOUNT").reason == "ACCOUNT_BINDING_MISMATCH"
assert entry_preflight(base, 1, "SINGLE_ACCOUNT").executable is False
assert EXISTING_DAILY_TRADE_LIMIT == 2
assert EARLIER_PLANNED_DAILY_TRADE_LIMIT == 1
settings = ResearchTimeSettings("15:45", 90)
assert settings.cutoff_new_entries == "15:45" and settings.maximum_holding_minutes == 90
print("MORTIFICATIO RISK CONTROLS PASS")
