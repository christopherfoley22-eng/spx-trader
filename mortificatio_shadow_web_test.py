"""Offline tests for the dedicated shadow button path."""

from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor

from mortificatio_shadow import ShadowLocalService
from executor_local_web import dispatch_shadow_post, shadow_execution_block


with tempfile.TemporaryDirectory() as directory:
    service = ShadowLocalService(directory)
    call = service.record_click("CALL", "click-call", "2026-01-01T14:00:00+00:00")
    put = service.record_click("PUT", "click-put", "2026-01-01T14:01:00+00:00")
    assert call["result"] == "RECORDED"
    assert put["result"] == "RECORDED"
    assert call["status"]["executable"] is False
    assert call["status"]["click_timestamp"] == "2026-01-01T14:00:00+00:00"
    assert call["status"]["rules"]
    duplicate = service.record_click("CALL", "click-call", "2026-01-01T14:00:00+00:00")
    assert duplicate["result"] == "DUPLICATE"
    assert service.status()["mode"] == "SHADOW / NO ORDERS"
    assert len(service.status()["decisions"]) == 2
    service.close()
    restarted = ShadowLocalService(directory)
    assert restarted.status()["decision"]["click_timestamp"] == "2026-01-01T14:01:00+00:00"
    assert restarted.status()["executable"] is False
    restarted.close()

ui = Path(__file__).with_name("executor_local_ui.html").read_text()
web = Path(__file__).with_name("executor_local_web.py").read_text()
assert "'/api/shadow/click'" in ui
assert "/api/shadow/observations" in web
assert "attach_observations" in web
assert "SHADOW_NO_EXECUTION" in web
with tempfile.TemporaryDirectory() as route_directory:
    route_service = ShadowLocalService(route_directory)
    assert shadow_execution_block(route_service, "/api/execute")["code"] == "SHADOW_NO_EXECUTION"
    assert shadow_execution_block(route_service, "/api/demo/intent")["code"] == "SHADOW_NO_EXECUTION"
    route_service.close()
assert 'ThreadingHTTPServer(("127.0.0.1", port)' in web
assert '"0.0.0.0"' not in web
assert "def log_message(self, *_):" in web
log_body=web.split("def log_message", 1)[1].split("def _host_ok", 1)[0]
assert "self.headers" not in log_body and "self.path" not in log_body
assert "renderShadowResults" in ui
assert "Option P&L" in ui
assert "P&L unavailable" in ui
assert "CONNECTING" in ui
assert "STATUS UNAVAILABLE" in ui
assert "Array.isArray(s.fixtures)?s.fixtures:[]" in ui
assert "click_timestamp" in ui
assert '"shadow"' in web
for forbidden in ("placeOrder", "cancelOrder", "exerciseOptions", "/api/order"):
    assert forbidden not in Path(__file__).with_name("mortificatio_shadow.py").read_text()

# Offline end-to-end route check: the shadow direction request is dispatched
# only to the shadow service, never to the simulation execute endpoint.
with tempfile.TemporaryDirectory() as directory:
    service = ShadowLocalService(directory)
    import json
    payload = dispatch_shadow_post(service, "/api/shadow/click", {"direction": "CALL",
        "request_id": "http-shadow-call", "click_timestamp": "2026-01-01T14:00:00+00:00"})
    assert payload["status"]["executable"] is False
    status = service.status()
    assert status["mode"] == "SHADOW / NO ORDERS"
    assert status["decision"]["direction"] == "CALL"
    assert dispatch_shadow_post(service, "/api/execute", {"direction": "CALL", "request_id": "wrong"}) is None
    def threaded_click():
        return dispatch_shadow_post(service, "/api/shadow/click", {"direction": "PUT",
            "request_id": "threaded-duplicate", "click_timestamp": "2026-01-01T14:02:00+00:00"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: threaded_click(), range(8)))
    assert sum(item["result"] == "RECORDED" for item in outcomes) == 1
    assert sum(item["result"] == "DUPLICATE" for item in outcomes) == 7
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: service.status(), range(8)))
    assert all(item["mode"] == "SHADOW / NO ORDERS" and item["executable"] is False for item in statuses)
    service.close()
print("MORTIFICATIO SHADOW BUTTON PATH PASS")
