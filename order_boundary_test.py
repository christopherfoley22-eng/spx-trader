"""Offline guard against adding broker order calls to this read-only repository."""

import ast
from pathlib import Path

FORBIDDEN = {
    "placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions",
    "replaceFA", "reqAutoOpenOrders", "Order", "placeOrderAsync",
    "cancelOrderAsync", "reqExecutions", "reqFundamentalData",
    "reqWshEventData", "reqWshMetaData", "reqFinancialAdvisorConfig",
    "requestFA", "updateAccountValue", "updatePortfolio",
    "setServerLogLevel",
}

MUTATION_API = {"placeOrder", "cancelOrder", "reqGlobalCancel",
                "exerciseOptions", "replaceFA", "reqAutoOpenOrders",
                "setServerLogLevel"}

root = Path(__file__).resolve().parent
probe_tree = ast.parse((root / "ibkr_read_only_validation.py").read_text())
probe_class = next(node for node in probe_tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "ReadOnlyProbe")
overrides = {node.name: node for node in probe_class.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
for method in MUTATION_API:
    assert method in overrides, ("ReadOnlyProbe must override broker mutation", method)
    body = overrides[method].body
    assert len(body) == 1 and isinstance(body[0], ast.Raise), ("Unsafe probe override", method)
bridge_tree = ast.parse((root / "ibkr_adapter_read_only_validation.py").read_text())
allowed_bridge_calls = {
    "connect", "reqAccountSummary", "reqPositions", "reqAllOpenOrders",
    "reqContractDetails", "reqSecDefOptParams", "reqMarketDataType",
    "reqMktData", "cancelPositions", "cancelAccountSummary",
    "cancelMktData", "disconnect", "isConnected", "safe_error_events",
}
for node in ast.walk(bridge_tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "app"):
        assert node.func.attr in allowed_bridge_calls, ("Unexpected TWS bridge API", node.func.attr)
diagnostic_tree = ast.parse((root / "ibkr_option_market_diagnostic.py").read_text())
allowed_diagnostic_calls = {
    "connect", "reqContractDetails", "reqSecDefOptParams",
    "reqMarketDataType", "reqMktData", "cancelMktData",
    "disconnect", "isConnected", "safe_error_events",
}
for node in ast.walk(diagnostic_tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "app"):
        assert node.func.attr in allowed_diagnostic_calls, ("Unexpected diagnostic TWS API", node.func.attr)
provenance_tree = ast.parse((root / "ibkr_market_provenance_validation.py").read_text())
allowed_provenance_calls = {
    "connect", "reqContractDetails", "reqSecDefOptParams", "reqMarketDataType",
    "reqMktData", "reqTickByTickData", "cancelTickByTickData", "cancelMktData",
    "isConnected", "disconnect", "result",
}
for node in ast.walk(provenance_tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "app"):
        assert node.func.attr in allowed_provenance_calls, (
            "Unexpected provenance TWS API", node.func.attr)
for name in ("executor_dry_run.py", "executor_local_service.py",
             "executor_local_web.py", "executor_ibkr_observation.py"):
    assert "ibkr_option_market_diagnostic" not in (root / name).read_text(), name
# The 125-contract model is isolated from the existing live/read-only/UI paths.
for name in ("executor_dry_run.py", "executor_local_service.py", "executor_local_web.py",
             "executor_ibkr_read_only.py", "ibkr_paper_read_only_validation.py"):
    assert "executor_portfolio" not in (root / name).read_text(), (name, "Portfolio execution boundary coupling")

# TradingView research must never become an input to broker readiness or control.
for name in ("executor_dry_run.py", "executor_protective_exits.py", "executor_market_ingestion.py",
             "executor_market_stream.py", "executor_paper_broker.py", "executor_paper_reconciliation.py",
             "executor_ibkr_read_only.py", "executor_local_web.py"):
    assert "executor_tradingview" not in (root / name).read_text(), (name, "TradingView authorization coupling")

# Stage 5A adds an opt-in transport without relaxing any existing prohibition.
stage5_tree = ast.parse((root / "ibkr_paper_read_only_validation.py").read_text())
read_requests = {"reqAccountSummary", "reqPositions", "reqAllOpenOrders",
                 "reqContractDetails", "reqSecDefOptParams", "reqMarketDataType", "reqMktData"}
for node in ast.walk(stage5_tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "request":
        assert node.args and isinstance(node.args[0], ast.Constant)
        assert node.args[0].value in read_requests, ("Unexpected Stage 5A request", node.lineno)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "app"):
        assert node.func.attr in {"connect", "run", "disconnect"}, ("Unexpected Stage 5A client API", node.lineno)

for path in root.glob("*.py"):
    tree = ast.parse(path.read_text(), filename=str(path))
    if path.name in {"executor_ibkr_observation.py", "executor_live_observation_service.py",
                     "executor_broker_snapshot.py", "executor_market_ingestion.py",
                     "executor_market_stream.py", "executor_ibkr_read_only.py",
                     "executor_portfolio.py", "executor_portfolio_liquidity.py",
                     "executor_tradingview.py", "executor_tradingview_replay.py", "executor_tradingview_web.py",
                     "executor_paper_broker.py", "executor_paper_reconciliation.py",
                     "executor_paper_types.py",
                     "executor_application.py", "executor_application_api.py",
                     "executor_allocation.py", "executor_allocation_service.py",
                     "executor_contract_selection.py",
                     "executor_lifecycle.py", "executor_offline_fixture.py",
                     "executor_adaptive_liquidity.py", "executor_liquidation.py",
                     "executor_liquidity_simulator.py", "executor_liquidity_benchmark.py",
                     "executor_exit_readiness.py", "executor_exit_stream.py", "executor_readiness_benchmark.py",
                     "executor_readiness_worker_benchmark.py",
                     "executor_entry_readiness.py", "executor_market_state.py", "executor_execution_coordinator.py",
                     "executor_execution_twin.py", "executor_phase10_benchmark.py",
                     "executor_exit_capacity.py", "executor_broker_reality.py", "executor_phase11_benchmark.py",
                     "executor_native_protection.py", "executor_broker_lifecycle_capture.py", "executor_phase12_benchmark.py",
                     "executor_trade_accounting.py", "executor_trade_history.py", "executor_blind_presentation.py",
                     "executor_dashboard_web.py",
                     "executor_protection_checkpoint.py", "mortificatio_credential_recovery.py",
                     "executor_protection_recovery.py",
                     "mortificatio_models.py", "mortificatio_store.py",
                     "mortificatio_auth.py", "mortificatio_protection.py",
                     "executor_redaction.py"}:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""])
                assert not any(name.startswith("ibapi") for name in names), (path.name, "Broker client import")
                assert "mortificatio_test_authenticator" not in names, (path.name, "Test authenticator in runtime")
            if isinstance(node, ast.Attribute):
                assert node.attr not in MUTATION_API, (path.name, node.lineno, node.attr)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if path.name=='ibkr_phase12_read_only.py' and node.func.attr=='reqExecutions':
                # Narrow observation-only exception, never an execution/mutation
                # permit. All other files retain the original prohibition.
                parents={child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
                parent=parents.get(node)
                while parent is not None and not isinstance(parent,ast.FunctionDef):parent=parents.get(parent)
                assert parent is not None and parent.name=='request_execution_history'
                assert isinstance(node.func.value,ast.Name) and node.func.value.id=='self'
            else:assert node.func.attr not in FORBIDDEN, (path.name, node.lineno, node.func.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in FORBIDDEN, (path.name, node.lineno, node.func.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"getattr", "setattr"} and len(node.args) >= 2:
                target = node.args[1]
                if isinstance(target, ast.Constant):
                    assert target.value not in FORBIDDEN, (path.name, node.lineno, target.value)
        if isinstance(node, ast.ImportFrom) and node.module == "ibapi.order":
            raise AssertionError((path.name, node.lineno, "Broker Order import"))

print("READ-ONLY ORDER BOUNDARY PASS")
