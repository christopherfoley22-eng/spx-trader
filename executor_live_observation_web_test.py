"""Offline handler boundary; no socket or IBKR transport is constructed."""

from types import SimpleNamespace

from executor_live_observation_service import LocalReadOnlyObservationService
from executor_local_web import make_handler

handler_type = make_handler(LocalReadOnlyObservationService())
handler = object.__new__(handler_type)
handler.server = SimpleNamespace(server_port=8765)
handler.headers = {"Host": "127.0.0.1:8765"}
received = []
handler._send = lambda code, body, *args, **kwargs: received.append((code, body))
handler.path = "/api/status"
handler.do_GET()
assert received[-1][0] == 200
assert received[-1][1]["mode"] == "READ-ONLY LIVE OBSERVATION / NO ORDERS"
assert received[-1][1]["state"] == "DISCONNECTED"
assert received[-1][1]["ready"] is False
for route in ("/api/intent", "/api/demo/load", "/api/demo/advance", "/api/demo/run"):
    handler.path = route
    handler.do_POST()
    assert received[-1][0] == 403
    assert received[-1][1]["code"] == "READ_ONLY_OBSERVATION"

print("READ-ONLY OBSERVATION HTTP HANDLER BOUNDARY PASS")
