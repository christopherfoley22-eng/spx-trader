"""Local status-only view of normalized read-only IBKR evidence.

There is intentionally no transport startup or intent method in this stage.
"""

from datetime import datetime, timezone
import time

from executor_ibkr_observation import ReadOnlyIBKREvidenceAdapter


class LocalReadOnlyObservationService:
    def __init__(self, adapter=None):
        self.adapter = adapter or ReadOnlyIBKREvidenceAdapter()

    def status(self):
        evidence = self.adapter.observe(datetime.now(timezone.utc), time.monotonic())
        return {"mode": "READ-ONLY LIVE OBSERVATION / NO ORDERS",
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
