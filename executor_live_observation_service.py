"""Local status-only view of normalized read-only IBKR evidence.

There is intentionally no transport startup or intent method in this stage.
"""

from datetime import datetime, timezone
import time

from executor_ibkr_observation import ReadOnlyIBKREvidenceAdapter
from simulation_evidence import EvidenceError


class LocalReadOnlyObservationService:
    def __init__(self, adapter=None, snapshot_machine=None, market_stream=None):
        self.adapter = adapter or ReadOnlyIBKREvidenceAdapter()
        self.snapshot_machine = snapshot_machine
        self.market_stream = market_stream

    def status(self):
        if self.snapshot_machine is not None:
            cycle = self.snapshot_machine.status(time.monotonic())
            return {"mode": "READ-ONLY LIVE OBSERVATION / NO ORDERS",
                    "state": "RECOVERY REQUIRED" if cycle["recovery_required"] else
                             "BLOCKED" if cycle["block_new_entry"] else "READ-ONLY CONNECTED",
                    "lifecycle": "OBSERVATION ONLY", "ready": False,
                    "reason": "; ".join(cycle["block_reasons"]) or "READ-ONLY RECONCILED FLAT",
                    "trade_count": None, "trade_limit": 2, "position": None,
                    "activity": [], "development_status": "OBSERVATION ONLY",
                    "fixture": None, "fixtures": [], "replay_index": None,
                    "replay_total": None, "snapshot": cycle,
                    "order_visibility": "API_VISIBLE_ONLY"}
        evidence = self.adapter.observe(datetime.now(timezone.utc), time.monotonic())
        result = {"mode": "READ-ONLY LIVE OBSERVATION / NO ORDERS",
                "state": evidence.state, "lifecycle": "OBSERVATION ONLY",
                "ready": False, "reason": "; ".join(evidence.reasons) or
                "MARKET DATA AND RECONCILIATION NOT PROVEN",
                "trade_count": None, "trade_limit": 2, "position": None,
                "activity": [], "development_status": "OBSERVATION ONLY",
                "fixture": None, "fixtures": [], "replay_index": None,
                "replay_total": None,
                "broker_position_observed": evidence.broker.position_qty if evidence.broker and evidence.broker.complete else None,
                "api_visible_open_order_count": evidence.broker.open_order_count if evidence.broker and evidence.broker.complete else None,
                "order_visibility": evidence.order_scope}
        if self.market_stream is not None:
            try:
                now_wall, now_mono = datetime.now(timezone.utc), time.monotonic()
                result["market_data"] = self.market_stream.rank(
                    self.market_stream.generation, now_wall, now_mono, evidence.account)
                result["market_callback_diagnostics"] = self.market_stream.diagnostics(
                    self.market_stream.generation, now_wall, now_mono)
            except EvidenceError as exc:
                result["market_data"] = {"executable": False, "selected": None,
                                         "block_reasons": [str(exc)]}
        return result
