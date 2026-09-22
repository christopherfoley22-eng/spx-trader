"""Loopback-only HTTP shell for the synthetic Executor service."""

import argparse
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets

from executor_local_service import LocalDryRunService, LocalServiceError
from executor_live_observation_service import LocalReadOnlyObservationService

HTML = Path(__file__).with_name("executor_local_ui.html").read_bytes()


def make_handler(service):
    auth_secret = secrets.token_urlsafe(32)
    csrf_secret = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ExecutorLocalDryRun"

        def log_message(self, *_):
            pass  # No request headers, cookies, account data, or secrets in logs.

        def _host_ok(self):
            return self.headers.get("Host") == "127.0.0.1:{}".format(self.server.server_port)

        def _send(self, code, body, content_type="application/json", cookie=False):
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            if cookie:
                self.send_header("Set-Cookie", "executor_local_session={}; HttpOnly; SameSite=Strict; Path=/".format(auth_secret))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, {"error": "Invalid local host"})
            if self.path == "/":
                page = HTML.replace(b"__CSRF_TOKEN__", csrf_secret.encode())
                return self._send(HTTPStatus.OK, page, "text/html; charset=utf-8", True)
            if self.path == "/api/status":
                return self._send(HTTPStatus.OK, service.status())
            return self._send(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, {"error": "Invalid local host"})
            if isinstance(service, LocalReadOnlyObservationService):
                return self._send(HTTPStatus.FORBIDDEN,
                                  {"error": "Observation mode has no mutating operations",
                                   "code": "READ_ONLY_OBSERVATION", "retryable": False})
            origin = "http://127.0.0.1:{}".format(self.server.server_port)
            cookie = self.headers.get("Cookie", "")
            token = self.headers.get("X-CSRF-Token", "")
            if (self.headers.get("Origin") != origin
                    or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}
                    or not hmac.compare_digest(cookie, "executor_local_session=" + auth_secret)
                    or not hmac.compare_digest(token, csrf_secret)):
                return self._send(HTTPStatus.FORBIDDEN, {"error": "Local session or CSRF check failed"})
            if self.headers.get("Content-Type") != "application/json":
                return self._send(HTTPStatus.BAD_REQUEST, {"error": "JSON required"})
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= 4096:
                    raise ValueError
                raw = json.loads(self.rfile.read(length))
                if not isinstance(raw, dict):
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                return self._send(HTTPStatus.BAD_REQUEST, {"error": "Malformed request"})
            try:
                if self.path == "/api/demo/intent" and set(raw) == {"direction", "request_id"}:
                    return self._send(HTTPStatus.OK, service.submit(raw["direction"], raw["request_id"]))
                if self.path == "/api/execute" and set(raw) == {"direction", "request_id"}:
                    return self._send(HTTPStatus.OK, service.execute(raw["direction"], raw["request_id"]))
                if self.path == "/api/demo/load" and set(raw) == {"fixture"}:
                    return self._send(HTTPStatus.OK, service.load_fixture(raw["fixture"]))
                if self.path == "/api/demo/advance" and not raw:
                    return self._send(HTTPStatus.OK, service.advance())
                if self.path == "/api/demo/run" and not raw:
                    return self._send(HTTPStatus.OK, service.advance(all_remaining=True))
                return self._send(HTTPStatus.BAD_REQUEST, {"error": "Unknown operation or fields"})
            except LocalServiceError as exc:
                return self._send(HTTPStatus.CONFLICT,
                                  {"error": str(exc), "code": exc.code,
                                   "retryable": exc.retryable, "status": service.status()})
            except Exception:
                return self._send(HTTPStatus.CONFLICT,
                                  {"error": "Recovery required", "code": "RECOVERY_REQUIRED",
                                   "retryable": False, "status": service.status()})

    return Handler


def make_server(service, port=8765):
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(service))


def main():
    parser = argparse.ArgumentParser(description="Local Executor DRY RUN / SIMULATION UI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", default=".executor-local")
    parser.add_argument("--mode", choices=("simulation", "observation"), default="simulation")
    args = parser.parse_args()
    service = (LocalDryRunService(args.state_dir) if args.mode == "simulation"
               else LocalReadOnlyObservationService())
    server = make_server(service, args.port)
    print("Executor {}: http://127.0.0.1:{}".format(
        "DRY RUN / SIMULATION" if args.mode == "simulation" else "READ-ONLY OBSERVATION",
        server.server_port))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if isinstance(service, LocalDryRunService):
            service.close()


if __name__ == "__main__":
    main()
