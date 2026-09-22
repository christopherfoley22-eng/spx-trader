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
for path in root.glob("*.py"):
    tree = ast.parse(path.read_text(), filename=str(path))
    if path.name in {"executor_ibkr_observation.py", "executor_live_observation_service.py",
                     "executor_broker_snapshot.py", "executor_market_ingestion.py",
                     "executor_redaction.py"}:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""])
                assert not any(name.startswith("ibapi") for name in names), (path.name, "Broker client import")
            if isinstance(node, ast.Attribute):
                assert node.attr not in MUTATION_API, (path.name, node.lineno, node.attr)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in FORBIDDEN, (path.name, node.lineno, node.func.attr)
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
