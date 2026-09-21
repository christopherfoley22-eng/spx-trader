"""Offline guard against adding broker order calls to this read-only repository."""

import ast
from pathlib import Path

FORBIDDEN = {
    "placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions",
    "replaceFA", "reqAutoOpenOrders", "Order",
}

root = Path(__file__).resolve().parent
for path in root.glob("*.py"):
    tree = ast.parse(path.read_text(), filename=str(path))
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
