"""Offline callback-order tests for the TWS-to-adapter bridge."""

from executor_ibkr_observation import ReadOnlyIBKREvidenceAdapter
from ibkr_adapter_read_only_validation import AdapterProbe

for order in ("accounts_first", "handshake_first"):
    adapter = ReadOnlyIBKREvidenceAdapter()
    probe = AdapterProbe(adapter)
    if order == "accounts_first":
        probe.managedAccounts("SYNTHETIC_ONLY")
        assert adapter.selected is None
        probe.nextValidId(1)
    else:
        probe.nextValidId(1)
        probe.managedAccounts("SYNTHETIC_ONLY")
    assert adapter.connected and adapter.selected == "SYNTHETIC_ONLY"
    assert adapter.accounts == ("SYNTHETIC_ONLY",)
    assert not probe.adapter_failure

adapter = ReadOnlyIBKREvidenceAdapter()
probe = AdapterProbe(adapter)
probe.managedAccounts("SYNTHETIC_ONLY")
probe.managedAccounts("SYNTHETIC_OTHER")
probe.nextValidId(1)
assert probe.adapter_failure and adapter.invalid == "CONTRADICTORY ACCOUNT EVIDENCE"

print("READ-ONLY TWS CALLBACK ORDER BRIDGE PASS")
