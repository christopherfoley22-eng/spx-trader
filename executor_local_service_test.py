"""Offline local-service and loopback HTTP safety tests."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from executor_local_service import LocalDryRunService, LocalServiceError
from executor_local_web import make_handler
from executor_replay_sessions import ACCOUNT, make_session


def request_id():
    return str(uuid.uuid4())


def scripted(name, direction, path, offset):
    raw = make_session(name, direction, list(path) + [path[-1]], quantity=1, fill_mode="SCRIPTED",
                       start_offset_seconds=offset)
    raw["events"][0]["fills"] = [{"event_id": name + ":entry", "side": "ENTRY",
                                   "chunk_index": 0, "quantity": 1, "rejected": False}]
    return raw


class LocalServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = LocalDryRunService(self.tmp.name)
        self.addCleanup(lambda: self.service.close())

    def test_calls_puts_idempotency_and_daily_cap_across_restart(self):
        self.assertTrue(self.service.status()["ready"])
        first = request_id()
        result = self.service.submit("CALL", first)
        self.assertEqual(result["status"]["lifecycle"], "OPEN")
        self.assertEqual(result["status"]["position"]["direction"], "CALL")
        self.assertEqual(result["status"]["position"]["quantity"], 11)
        self.assertEqual(self.service.submit("CALL", first)["result"], "DUPLICATE")
        with self.assertRaises(LocalServiceError):
            self.service.submit("PUT", first)
        with self.assertRaises(LocalServiceError):
            self.service.submit("PUT", request_id())
        self.assertFalse(self.service.status()["ready"])
        self.service.close()
        self.service = LocalDryRunService(self.tmp.name)
        self.assertEqual(self.service.status()["trade_count"], 1)
        self.assertEqual(self.service.status()["lifecycle"], "OPEN")
        self.assertEqual(self.service.advance(all_remaining=True)["lifecycle"], "FLAT")
        self.assertTrue(self.service.status()["ready"])
        second = self.service.submit("PUT", request_id())
        self.assertEqual(second["status"]["position"]["direction"], "PUT")
        self.assertEqual(second["status"]["state"], "OPEN")
        self.service.close()
        self.service = LocalDryRunService(self.tmp.name)
        self.assertEqual(self.service.status()["trade_count"], 2)
        self.service.advance(all_remaining=True)
        self.assertEqual(self.service.status()["state"], "BLOCKED")
        with self.assertRaises(LocalServiceError):
            self.service.submit("CALL", request_id())

    def test_rapid_and_simultaneous_intents_create_one_position(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda direction: self._attempt(direction),
                                    ("CALL", "PUT")))
        self.assertEqual(sum(result == "ACCEPTED" for result in results), 1)
        self.assertEqual(self.service.status()["trade_count"], 1)
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM requests").fetchone()[0], 1)
        with self.assertRaises(LocalServiceError):
            self.service.submit("CALL", request_id())

    def _intent_arriving_during_run_cannot_be_deferred(self, first_direction, overlapping_direction):
        self.service.submit(first_direction, request_id())
        original = self.service._progress
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()

        def held_progress(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Run gate timed out")
            return original(*args, **kwargs)

        self.service._progress = held_progress
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                running = pool.submit(self.service.advance, all_remaining=True)
                self.assertTrue(entered.wait(5))

                def overlapping_intent():
                    attempted.set()
                    try:
                        self.service.submit(overlapping_direction, request_id())
                    except LocalServiceError as exc:
                        return exc.code
                    return "ACCEPTED"

                other = pool.submit(overlapping_intent)
                self.assertTrue(attempted.wait(5))
                self.assertEqual(other.result(timeout=1), "ACTION_IN_PROGRESS")
                release.set()
                self.assertEqual(running.result(timeout=5)["lifecycle"], "FLAT")
        finally:
            release.set()
            self.service._progress = original
        self.assertEqual(self.service.status()["trade_count"], 1)
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM requests").fetchone()[0], 1)

    def test_put_arriving_during_call_run_is_rejected(self):
        self._intent_arriving_during_run_cannot_be_deferred("CALL", "PUT")

    def test_call_arriving_during_put_run_is_rejected(self):
        self._intent_arriving_during_run_cannot_be_deferred("PUT", "CALL")

    def test_duplicate_run_during_active_replay_is_idempotent(self):
        self.service.submit("CALL", request_id())
        original = self.service._progress
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()

        def held_progress(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Run gate timed out")
            return original(*args, **kwargs)

        self.service._progress = held_progress
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.service.advance, all_remaining=True)
                self.assertTrue(entered.wait(5))

                def repeated_run():
                    second_started.set()
                    try:
                        self.service.advance(all_remaining=True)
                    except LocalServiceError as exc:
                        return exc.code
                    return "REPLAYED"

                second = pool.submit(repeated_run)
                self.assertTrue(second_started.wait(5))
                release.set()
                self.assertEqual(first.result(timeout=5)["lifecycle"], "FLAT")
                self.assertEqual(second.result(timeout=5), "INTENT_NOT_READY_RETRY")
        finally:
            release.set()
            self.service._progress = original
        with sqlite3.connect(self.service.executor_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE kind='REPLAY_EVENT_COMPLETE'").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE kind='EXIT_EVENT'").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT broker_qty FROM controller").fetchone()[0], 0)
        self.assertEqual(self.service.status()["trade_count"], 1)

    def _attempt(self, direction):
        try:
            return self.service.submit(direction, request_id())["result"]
        except LocalServiceError:
            return "BLOCKED"

    def test_entering_exiting_and_recovery_states(self):
        self.service.close()
        self.service = LocalDryRunService(self.tmp.name, session_factory=scripted)
        raw_id = request_id()
        self.service.submit("CALL", raw_id)
        self.assertEqual(self.service.status()["lifecycle"], "OPEN")
        with self.assertRaises(LocalServiceError):
            self.service.submit("PUT", request_id())
        self.service.advance()
        self.assertEqual(self.service.status()["lifecycle"], "OPEN")
        self.service.advance()
        self.assertEqual(self.service.status()["lifecycle"], "EXITING")
        with self.assertRaises(LocalServiceError):
            self.service.submit("CALL", request_id())
        self.service.close()
        self.service = LocalDryRunService(self.tmp.name, session_factory=scripted)
        self.assertEqual(self.service.status()["lifecycle"], "EXITING")
        self.assertEqual(self.service.status()["trade_count"], 1)
        self.assertEqual(self.service.status()["position"]["quantity"], 1)
        self.assertEqual(self.service.status()["state"], "EXITING")
        with self.assertRaises(LocalServiceError) as ended:
            self.service.advance()
        self.assertEqual(ended.exception.code, "REPLAY_ENDED_ACTIVE_POSITION")
        self.assertEqual(self.service.status()["state"], "RECOVERY REQUIRED")
        self.assertFalse(self.service.status()["ready"])

    def test_entering_blocks_new_intent(self):
        def no_fills(name, direction, path, offset):
            return make_session(name, direction, path, fill_mode="SCRIPTED",
                                start_offset_seconds=offset)
        self.service.close()
        self.service = LocalDryRunService(self.tmp.name, session_factory=no_fills)
        self.service.submit("PUT", request_id())
        self.assertEqual(self.service.status()["lifecycle"], "ENTERING")
        self.assertEqual(self.service.status()["trade_count"], 0)
        with self.assertRaises(LocalServiceError):
            self.service.submit("CALL", request_id())

    def test_stale_reconciliation_and_corruption_fail_closed(self):
        def stale(name, direction, path, offset):
            raw = make_session(name, direction, path, start_offset_seconds=offset)
            raw["events"][0]["spx"]["source_time"] = "2026-09-21T09:29:58-04:00"
            return raw
        self.service.close()
        self.service = LocalDryRunService(self.tmp.name, session_factory=stale)
        self.assertFalse(self.service.status()["ready"])
        with self.assertRaises(LocalServiceError):
            self.service.submit("CALL", request_id())
        self.service.close()
        def wrong_broker(name, direction, path, offset):
            raw = make_session(name, direction, path, start_offset_seconds=offset)
            mono = raw["events"][0]["monotonic"]
            raw["events"][0]["broker"] = {
                "account_id": ACCOUNT, "selected_account": ACCOUNT,
                "managed_accounts": [ACCOUNT], "complete": True,
                "position_qty": 1, "con_id": 999, "open_order_count": 0,
                "received_monotonic": mono}
            return raw
        self.service = LocalDryRunService(self.tmp.name, session_factory=wrong_broker)
        self.assertFalse(self.service.status()["ready"])
        self.service.close()
        self.service = LocalDryRunService(self.tmp.name)
        self.service.submit("CALL", request_id())
        with sqlite3.connect(self.service.executor_path) as db:
            db.execute("UPDATE controller SET trades_today=0")
        self.assertEqual(self.service.status()["state"], "RECOVERY REQUIRED")
        with self.assertRaises(Exception):
            self.service.submit("PUT", request_id())

    def test_malformed_ids_and_no_user_configuration(self):
        for direction, rid in (("SELL", request_id()), ("CALL", "abc"),
                               ("PUT", None), ("PUT", "00000000-0000-0000-0000-000000000000")):
            with self.assertRaises(LocalServiceError):
                self.service.submit(direction, rid)
        self.assertEqual(self.service.status()["trade_count"], 0)

    def test_completed_replay_journal_must_verify_before_next_intent(self):
        self.service.submit("CALL", request_id())
        self.service.advance(all_remaining=True)
        self.assertTrue(self.service.status()["ready"])
        with sqlite3.connect(self.service.executor_path) as db:
            db.execute("UPDATE audit SET detail='{}' WHERE kind='HIGH_WATER'")
        self.assertEqual(self.service.status()["state"], "RECOVERY REQUIRED")
        with self.assertRaises(LocalServiceError):
            self.service.submit("PUT", request_id())


class LocalHTTPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = LocalDryRunService(self.tmp.name)
        self.addCleanup(self.service.close)
        self.handler = make_handler(self.service)
        self.port = 8765
        self.origin = "http://127.0.0.1:{}".format(self.port)

    def call(self, method, path, body=None, headers=None):
        payload = b"" if body is None else json.dumps(body).encode()
        all_headers = {"Host": "127.0.0.1:{}".format(self.port), **(headers or {})}
        all_headers["Content-Length"] = str(len(payload))
        request = ("{} {} HTTP/1.1\r\n".format(method, path)
                   + "".join("{}: {}\r\n".format(k, v) for k, v in all_headers.items())
                   + "\r\n").encode() + payload

        class Connection:
            def __init__(self, data):
                self.input = io.BytesIO(data)
                self.output = io.BytesIO()

            def makefile(self, mode, *_):
                return self.input if "r" in mode else self.output

            def sendall(self, data):
                self.output.write(data)

        class Server:
            server_port = 8765

        connection = Connection(request)
        self.handler(connection, ("127.0.0.1", 1), Server())
        raw = connection.output.getvalue()
        head, result = raw.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        code = int(lines[0].split()[1])
        cookie = next((line.split(": ", 1)[1] for line in lines
                       if line.lower().startswith("set-cookie:")), None)
        return code, result, cookie

    def auth(self):
        code, body, cookie = self.call("GET", "/")
        self.assertEqual(code, 200)
        import re
        token = re.search(rb'content="([A-Za-z0-9_-]+)"', body)
        self.assertIsNotNone(token)
        return {"Content-Type": "application/json", "Origin": self.origin,
                "Cookie": cookie.split(";", 1)[0],
                "X-CSRF-Token": token.group(1).decode()}

    def test_http_auth_csrf_and_server_side_gate(self):
        rid = request_id()
        body = {"direction": "CALL", "request_id": rid}
        self.assertEqual(self.call("POST", "/api/intent", body,
                                   {"Content-Type": "application/json"})[0], 403)
        headers = self.auth()
        bad_origin = dict(headers, Origin="http://evil.example")
        self.assertEqual(self.call("POST", "/api/intent", body, bad_origin)[0], 403)
        bad_csrf = dict(headers, **{"X-CSRF-Token": "wrong"})
        self.assertEqual(self.call("POST", "/api/intent", body, bad_csrf)[0], 403)
        self.assertEqual(self.call("POST", "/api/intent", body, headers)[0], 200)
        self.assertEqual(self.call("POST", "/api/intent", body, headers)[0], 200)
        self.assertEqual(self.call("POST", "/api/intent",
                                   {"direction": "PUT", "request_id": request_id()}, headers)[0], 409)
        self.assertEqual(self.call("POST", "/api/intent",
                                   {"direction": "CALL", "request_id": request_id(),
                                    "quantity": 25}, headers)[0], 400)
        for path in ("/api/sell", "/api/exit", "/api/order", "/api/preview"):
            self.assertEqual(self.call("POST", path, {}, headers)[0], 400)
        status = json.loads(self.call("GET", "/api/status")[1])
        self.assertEqual(status["lifecycle"], "OPEN")
        self.assertEqual(status["trade_count"], 1)
        self.assertNotIn(ACCOUNT.encode(), self.call("GET", "/")[1])
        self.assertNotIn(ACCOUNT, json.dumps(status))

    def _assert_immediate_winner_flat(self, direction):
        status = json.loads(self.call("GET", "/api/status")[1])
        self.assertEqual(status["state"], "CONFIRMED FLAT" if status["trade_count"] == 1
                         else "BLOCKED")
        self.assertEqual(status["lifecycle"], "FLAT")
        with sqlite3.connect(self.service.executor_path) as db:
            rows = db.execute("SELECT kind,detail FROM audit ORDER BY rowid").fetchall()
            kinds = [kind for kind, _ in rows]
            self.assertEqual(kinds.count("TRADE_COUNTED"), status["trade_count"])
            self.assertEqual(kinds.count("CONFIRMED_FLAT"), status["trade_count"])
            self.assertEqual(kinds.count("LET_IT_RIDE"), status["trade_count"])
            self.assertEqual(kinds.count("ENTRY_EVENT"), 2 * status["trade_count"])
            self.assertEqual(kinds.count("EXIT_EVENT"), 2 * status["trade_count"])
            self.assertEqual(sum(json.loads(detail)["fill"] for kind, detail in rows
                                 if kind == "ENTRY_EVENT"), 11 * status["trade_count"])
            self.assertEqual(sum(json.loads(detail)["fill"] for kind, detail in rows
                                 if kind == "EXIT_EVENT"), 11 * status["trade_count"])
            triggers = [json.loads(detail) for kind, detail in rows
                        if kind == "EXIT_TRIGGERED"]
            self.assertEqual(triggers[-1]["reason"], "LET_IT_RIDE_REVERSAL")
            self.assertEqual(triggers[-1]["reversal"], "3.0")
            self.assertEqual(db.execute("SELECT broker_qty FROM controller").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM completed_trades").fetchone()[0],
                             status["trade_count"])
            self.assertEqual(db.execute("SELECT direction FROM completed_trades ORDER BY rowid DESC LIMIT 1").fetchone()[0],
                             direction)
            self.assertIn("reconcile:flat:", " ".join(
                x[0] for x in db.execute("SELECT event_id FROM audit WHERE kind='RECONCILED'")))
            self.assertEqual(db.execute("SELECT last_complete_index FROM replay_progress").fetchone()[0], 2)

    def _exact_immediate_winner_sequence(self, direction):
        headers = self.auth()
        code, _, _ = self.call("POST", "/api/demo/load",
                               {"fixture": "immediate_winner"}, headers)
        self.assertEqual(code, 200)
        before = json.loads(self.call("GET", "/api/status")[1])
        self.assertEqual((before["state"], before["lifecycle"], before["trade_count"]),
                         ("READY", "FLAT", 0))
        rid = request_id()
        code, raw, _ = self.call("POST", "/api/intent",
                                 {"direction": direction, "request_id": rid}, headers)
        self.assertEqual(code, 200, raw)
        entered = json.loads(self.call("GET", "/api/status")[1])
        self.assertEqual((entered["lifecycle"], entered["replay_index"],
                          entered["position"]["quantity"], entered["trade_count"]),
                         ("OPEN", 0, 11, 1))
        self.assertEqual(self.call("POST", "/api/intent",
                                   {"direction": direction, "request_id": rid}, headers)[0], 200)
        code, raw, _ = self.call("POST", "/api/demo/run", {}, headers)
        self.assertEqual(code, 200, raw)
        self._assert_immediate_winner_flat(direction)
        with sqlite3.connect(self.service.executor_path) as db:
            observations = [json.loads(detail)["price"] for (detail,) in db.execute(
                "SELECT detail FROM audit WHERE kind='MARKET_OBSERVED' ORDER BY rowid")]
            self.assertEqual(observations, [5000.0, 5005.0 if direction == "CALL"
                                            else 4995.0, 5002.0 if direction == "CALL"
                                            else 4998.0])
            event_count = db.execute("SELECT count(*) FROM audit").fetchone()[0]
        code, raw, _ = self.call("POST", "/api/demo/run", {}, headers)
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(raw)["code"], "INTENT_NOT_READY_RETRY")
        with sqlite3.connect(self.service.executor_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit").fetchone()[0], event_count)
            self.assertEqual(db.execute("SELECT broker_qty FROM controller").fetchone()[0], 0)
        self.assertEqual(json.loads(self.call("GET", "/api/status")[1])["trade_count"], 1)

    def test_exact_immediate_winner_call_sequence(self):
        self._exact_immediate_winner_sequence("CALL")

    def test_exact_immediate_winner_put_sequence(self):
        self._exact_immediate_winner_sequence("PUT")

    def test_run_before_intent_is_explicit_retryable_then_mirrored_recovery(self):
        headers = self.auth()
        for direction in ("CALL", "PUT"):
            with self.subTest(direction=direction):
                load_code, _, _ = self.call("POST", "/api/demo/load",
                                            {"fixture": "immediate_winner"}, headers)
                self.assertEqual(load_code, 200)
                self.assertTrue(json.loads(self.call("GET", "/api/status")[1])["ready"])
                original = self.service.submit
                started, release = threading.Event(), threading.Event()

                def delayed_submit(*args):
                    started.set()
                    if not release.wait(5):
                        raise AssertionError("Test intent gate timed out")
                    return original(*args)

                self.service.submit = delayed_submit
                try:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        intent = pool.submit(self.call, "POST", "/api/intent",
                                             {"direction": direction,
                                              "request_id": request_id()}, headers)
                        self.assertTrue(started.wait(5))
                        early_code, early_raw, _ = self.call("POST", "/api/demo/run", {}, headers)
                        early = json.loads(early_raw)
                        self.assertEqual(early_code, 409)
                        self.assertEqual(early["code"], "INTENT_NOT_READY_RETRY")
                        self.assertTrue(early["retryable"])
                        self.assertEqual(early["status"]["state"],
                                         "READY" if direction == "CALL" else "CONFIRMED FLAT")
                        release.set()
                        accepted_code, accepted_raw, _ = intent.result(timeout=5)
                finally:
                    release.set()
                    self.service.submit = original
                self.assertEqual(accepted_code, 200)
                self.assertEqual(json.loads(accepted_raw)["status"]["lifecycle"], "OPEN")
                code, raw, _ = self.call("POST", "/api/demo/run", {}, headers)
                self.assertEqual(code, 200, raw)
                self._assert_immediate_winner_flat(direction)

    def test_run_waits_for_inflight_intent_and_repeated_clicks_are_idempotent(self):
        headers = self.auth()
        original = self.service._progress
        entered, release, run_arrived = threading.Event(), threading.Event(), threading.Event()

        def delayed_progress(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test progress gate timed out")
            return original(*args, **kwargs)

        original_advance = self.service.advance
        def flagged_advance(*args, **kwargs):
            run_arrived.set()
            return original_advance(*args, **kwargs)

        self.service._progress = delayed_progress
        self.service.advance = flagged_advance
        rid = request_id()
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(self.call, "POST", "/api/intent",
                                    {"direction": "CALL", "request_id": rid}, headers)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.call, "POST", "/api/intent",
                                     {"direction": "CALL", "request_id": rid}, headers)
                run = pool.submit(self.call, "POST", "/api/demo/run", {}, headers)
                self.assertTrue(run_arrived.wait(5))
                self.assertFalse(run.done())
                release.set()
                first_code, first_raw, _ = first.result(timeout=5)
                second_code, second_raw, _ = second.result(timeout=5)
                run_code, run_raw, _ = run.result(timeout=5)
        finally:
            release.set()
            self.service._progress = original
            self.service.advance = original_advance
        self.assertEqual(first_code, 200)
        self.assertEqual(json.loads(first_raw)["result"], "ACCEPTED")
        self.assertIn(second_code, {200, 409})
        if second_code == 409:
            self.assertEqual(json.loads(second_raw)["code"], "ACTION_IN_PROGRESS")
            self.assertTrue(json.loads(second_raw)["retryable"])
        else:
            self.assertEqual(json.loads(second_raw)["result"], "DUPLICATE")
        self.assertEqual(run_code, 200, run_raw)
        retry_code, retry_raw, _ = self.call("POST", "/api/intent",
                                             {"direction": "CALL", "request_id": rid}, headers)
        self.assertEqual(retry_code, 200)
        self.assertEqual(json.loads(retry_raw)["result"], "DUPLICATE")
        self._assert_immediate_winner_flat("CALL")
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM requests").fetchone()[0], 1)

    def test_end_of_replay_with_open_position_is_not_completion(self):
        headers = self.auth()
        self.assertEqual(self.call("POST", "/api/demo/load",
                                   {"fixture": "exact_five_transition"}, headers)[0], 200)
        self.assertEqual(self.call("POST", "/api/intent",
                                   {"direction": "CALL", "request_id": request_id()}, headers)[0], 200)
        code, raw, _ = self.call("POST", "/api/demo/run", {}, headers)
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(raw)["code"], "REPLAY_ENDED_ACTIVE_POSITION")
        status = json.loads(self.call("GET", "/api/status")[1])
        self.assertEqual(status["state"], "RECOVERY REQUIRED")
        self.assertEqual(status["lifecycle"], "OPEN")
        self.assertEqual(status["development_status"], "REPLAY ENDED WITH ACTIVE POSITION")
        self.assertFalse(status["ready"])
        self.assertEqual(self.call("POST", "/api/intent",
                                   {"direction": "PUT", "request_id": request_id()}, headers)[0], 409)
        with sqlite3.connect(self.service.executor_path) as db:
            self.assertEqual(db.execute("SELECT broker_qty FROM controller").fetchone()[0], 11)
            self.assertEqual(db.execute("SELECT count(*) FROM completed_trades").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
