"""Synthetic, explicitly labeled tests for the offline risk-budget calculator."""
from mortificatio_risk_budget import BudgetInput, ExitScenario, calculate_budget

synthetic = BudgetInput("10000", "2.00", 25, (
    ExitScenario("PLANNED_EXIT", "1.00"),
    ExitScenario("WORSE_EXIT_WITH_SLIPPAGE", "0.50", "0.05", "0.10"),
))
report = calculate_budget(synthetic)
assert report["cash_sufficient"] is True and report["executable"] is False
assert report["proposed_daily_ceiling"] is None
assert report["scenarios"][0]["one_trade_loss"] == "2500.00"
assert report["scenarios"][0]["two_trade_loss"] == "5000.00"
assert report["scenarios"][1]["dollar_loss"] == "4125.00"
assert report["scenarios"][1]["ceiling_status"] == "UNSET"
limited = BudgetInput("10000", "2.00", 25, (ExitScenario("WORSE", "0.50"),), "4000")
assert calculate_budget(limited)["scenarios"][0]["ceiling_status"] == "BLOCKED"
assert calculate_budget(BudgetInput("10000", "2.00", 25, (ExitScenario("OK", "1.50"),), "3000"))["scenarios"][0]["ceiling_status"] == "ELIGIBLE"
for bad in (0, 26):
    try: calculate_budget(BudgetInput("10000", "2.00", bad, (ExitScenario("X", "1"),)))
    except ValueError: pass
    else: raise AssertionError("invalid contract count accepted")
for bad in (None, (), []):
    try: calculate_budget(BudgetInput("10000", "2.00", 1, bad))
    except ValueError: pass
    else: raise AssertionError("missing scenarios accepted")
print("MORTIFICATIO RISK BUDGET PASS (SYNTHETIC)")
