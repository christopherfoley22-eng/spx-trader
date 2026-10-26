"""UI status/EXIT ALL shell for an injected OFFLINE protective manager only."""

import uuid
from decimal import Decimal

from executor_local_service import LocalServiceError


class LocalProtectiveExitService:
    def __init__(self, manager):
        self.manager = manager

    def exit_all(self, request_id):
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise LocalServiceError("Stable emergency UUID required")
        # Persist independently before waiting for the manager/reconciliation lock.
        if self.manager.emergency is not None:
            self.manager.emergency.request(request_id)
        result = self.manager.exit_all(request_id)
        return {"result": result, "executable": False, "emergency": {"latched": True}}

    def status(self):
        model = self.manager.status()
        position = None
        if model.get("quantity"):
            contract = model["contract"]
            peak = Decimal(self.manager.state["peak"])
            entry = Decimal(self.manager.state["entry_spx"])
            favorable = peak-entry if contract["right"] == "C" else entry-peak
            ride = self.manager.state["ride"]
            early = not ride and favorable >= Decimal("4")
            position = {"direction": "CALL" if contract["right"] == "C" else "PUT",
                        "quantity": model["quantity"], "strike": contract["strike"],
                        "simulated_entry_ask": model["average_entry_premium"],
                        "entry_spx": self.manager.state["entry_spx"], "high_water_spx": self.manager.state["peak"],
                        "maximum_favorable_points": str(favorable), "strategy": "OFFLINE_PROTECTIVE_MODEL",
                        "average_entry_premium": model["average_entry_premium"],
                        "initial_stop_premium": model["stop_price"],
                        "fixed_profit_floor_points": "1.25" if early else None,
                        "early_protection_active": early, "trailing_active": ride,
                        "trailing_threshold_spx": str(peak + (Decimal("-3") if contract["right"] == "C" else Decimal("3"))) if ride else None,
                        "exit_reason": self.manager.state["exit_reason"]}
        return {"mode": "OFFLINE PROTECTIVE MODEL / NO ORDERS", "state": model["phase"],
                "lifecycle": "FLAT" if model["phase"] == "FLAT CONFIRMED" else "EXITING" if model["emergency"] or self.manager.state["exit_reason"] else "OPEN",
                "ready": False, "reason": "Offline model only; no order transport exists",
                "trade_count": None, "position": position, "activity": [], "fixtures": [],
                "fixture": None, "replay_index": None, "replay_total": None,
                "protective_exit_model": model, "emergency": {"latched": model["emergency"]}, "executable": False}
