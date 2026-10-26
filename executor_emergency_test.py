"""Offline durable EXIT ALL, controller integration, HTTP and concurrency tests."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from executor_emergency import EmergencyLatch
from executor_dry_run import DryRunError, DryRunExecutor
from executor_local_service import LocalDryRunService, LocalServiceError
import executor_local_service_test as local_http_helpers
from executor_local_web import make_handler
from executor_live_observation_service import LocalReadOnlyObservationService
from executor_replay import ReplaySession
from executor_replay_process import DurableReplayRunner
from executor_replay_sessions import ACCOUNT, make_session
import executor_protective_exits_test as protective_fixtures
from executor_protective_service import LocalProtectiveExitService


class EmergencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_latch_survives_restart_and_duplicate_after_confirmation_is_idempotent(self):
        latch = EmergencyLatch(self.root/"emergency.sqlite", ACCOUNT)
        rid = str(uuid.uuid4())
        self.assertFalse(latch.blocked())
        latch.request(rid)
        latch = EmergencyLatch(latch.path, ACCOUNT)
        self.assertTrue(latch.blocked())
        latch.confirm_flat("synthetic-reconciled-proof")
        self.assertEqual(latch.request(rid), "DUPLICATE")
        self.assertEqual(latch.status()["phase"], "FLAT_CONFIRMED")

    def test_exit_all_does_not_wait_for_the_service_replay_lock(self):
        service = LocalDryRunService(self.root)
        self.addCleanup(service.close)
        with service.lock, ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(service.exit_all, str(uuid.uuid4())).result(timeout=2)
        self.assertTrue(result["emergency"]["latched"])
        with self.assertRaises(LocalServiceError):
            service.execute("CALL", str(uuid.uuid4()))
        self.assertNotEqual(service.status()["lifecycle"], "FLAT")

    def test_active_auto_fixture_exit_all_uses_reconciled_simulated_fills(self):
        service = LocalDryRunService(self.root)
        self.addCleanup(service.close)
        service.submit("CALL", str(uuid.uuid4()))
        rid = str(uuid.uuid4())
        service.exit_all(rid)
        status = service.status()
        self.assertEqual(status["state"], "EMERGENCY / FLAT CONFIRMED")
        self.assertFalse(status["ready"])
        controller = service._controller()
        try:
            self.assertEqual(controller.completed_trades()[0][5], "EMERGENCY_EXIT_ALL")
            self.assertEqual(controller.status().owned, 0)
        finally:
            controller.close()
        service.exit_all(rid)
        self.assertEqual(service.status()["state"], "EMERGENCY / FLAT CONFIRMED")

    def test_restart_after_click_before_controller_application_preserves_entry_block(self):
        service = LocalDryRunService(self.root)
        service.submit("PUT", str(uuid.uuid4()))
        service.exit_all(str(uuid.uuid4()))
        service.close()
        service = LocalDryRunService(self.root)
        self.addCleanup(service.close)
        with self.assertRaises(LocalServiceError):
            service.submit("CALL", str(uuid.uuid4()))
        self.assertEqual(service.status()["state"], "EMERGENCY / FLAT CONFIRMED")

    def test_exhausted_open_fixture_cannot_invent_a_fill_or_flatness(self):
        service = LocalDryRunService(self.root)
        self.addCleanup(service.close)
        service.load_fixture("exact_five_transition")
        service.submit("CALL", str(uuid.uuid4()))
        with self.assertRaises(LocalServiceError):
            service.advance(all_remaining=True)
        service.exit_all(str(uuid.uuid4()))
        status = service.status()
        self.assertEqual(status["state"], "EMERGENCY EXIT PENDING")
        self.assertNotEqual(status["lifecycle"], "FLAT")
        self.assertEqual(status["position"]["quantity"], 11)

    def test_partial_entry_emergency_exits_only_the_actual_filled_quantity(self):
        raw = make_session("partial-emergency", "PUT", [0], fill_mode="SCRIPTED")
        raw["events"][0]["fills"] = [{"event_id": "entry-four", "side": "ENTRY", "chunk_index": 0,
                                      "quantity": 4, "rejected": False}]
        raw["events"][1]["fills"] = [{"event_id": "exit-four", "side": "EXIT", "chunk_index": 0,
                                      "quantity": 4, "rejected": False}]
        session = ReplaySession.from_dict(raw)
        latch = EmergencyLatch(self.root/"emergency.sqlite", ACCOUNT)
        runner = DurableReplayRunner(session, self.root/"controller.sqlite", emergency=latch)
        runner.run(stop_index=0)
        self.assertEqual(runner.controller.status().owned, 4)
        runner.close()
        latch.request(str(uuid.uuid4()))
        runner = DurableReplayRunner(session, self.root/"controller.sqlite", emergency=latch)
        try:
            result = runner.run()
            self.assertEqual(result["phase"], "FLAT")
            self.assertEqual(sum(fill["quantity"] for fill in result["exit_fills"]), 4)
            self.assertEqual(runner.controller.completed_trades()[0][4], 4)
            self.assertEqual(latch.status()["phase"], "FLAT_CONFIRMED")
        finally:
            runner.close()

    def test_latch_before_first_event_never_acquires_contracts(self):
        session = ReplaySession.from_dict(make_session("abort-before-entry", "CALL", [5, 2]))
        latch = EmergencyLatch(self.root/"emergency.sqlite", ACCOUNT)
        latch.request(str(uuid.uuid4()))
        runner = DurableReplayRunner(session, self.root/"controller.sqlite", emergency=latch)
        try:
            result = runner.run()
            self.assertEqual(result["entry_fills"], [])
            self.assertEqual(result["trade_count"], 0)
            self.assertEqual(result["phase"], "FLAT")
        finally:
            runner.close()

    def test_latch_metadata_alone_never_proves_broker_flatness(self):
        service = LocalDryRunService(self.root)
        self.addCleanup(service.close)
        service.exit_all(str(uuid.uuid4()))
        service.emergency.confirm_flat("unverified-string-is-not-a-broker-snapshot")
        self.assertEqual(service.status()["state"], "EMERGENCY EXIT PENDING")
        self.assertNotEqual(service.status()["lifecycle"], "FLAT")

    def test_controller_journal_blocks_entry_even_without_external_latch_after_restart(self):
        raw = make_session("journal-guard", "CALL", [5, 2])
        session = ReplaySession.from_dict(raw)
        latch = EmergencyLatch(self.root/"emergency.sqlite", ACCOUNT)
        runner = DurableReplayRunner(session, self.root/"controller.sqlite", emergency=latch)
        runner.run(stop_index=0)
        latch.request(str(uuid.uuid4()))
        runner.run()
        runner.close()
        engine = DryRunExecutor(self.root/"controller.sqlite", ACCOUNT, lambda _: True)
        try:
            event = session.events[-1]
            from simulation_evidence import VerifiedBrokerSnapshot
            snapshot = VerifiedBrokerSnapshot(ACCOUNT, ACCOUNT, (ACCOUNT,), True, 0, None, 0, event.monotonic)
            with self.assertRaises(DryRunError):
                engine.press("new-after-emergency", "CALL", snapshot, event.now_wall, event.monotonic)
        finally:
            engine.close()


class EmergencyHTTPTest(unittest.TestCase):
    call, auth = local_http_helpers.LocalHTTPTest.call, local_http_helpers.LocalHTTPTest.auth

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = LocalDryRunService(self.tmp.name)
        self.addCleanup(self.service.close)
        self.handler = make_handler(self.service)
        self.port, self.origin = 8765, "http://127.0.0.1:8765"

    def test_emergency_route_requires_auth_and_preserves_execution_boundaries(self):
        payload = {"request_id": str(uuid.uuid4())}
        self.assertEqual(self.call("POST", "/api/exit-all", payload)[0], 403)
        headers = self.auth()
        self.assertEqual(self.call("POST", "/api/exit-all", payload, dict(headers, Origin="http://evil.example"))[0], 403)
        self.assertEqual(self.call("POST", "/api/exit-all", dict(payload, quantity=25), headers)[0], 400)
        self.assertEqual(self.call("POST", "/api/exit-all", payload, headers)[0], 200)
        self.assertEqual(self.call("POST", "/api/exit-all", payload, headers)[0], 200)
        self.assertEqual(self.call("POST", "/api/execute", {"direction": "CALL", "request_id": str(uuid.uuid4())}, headers)[0], 409)
        self.assertEqual(self.call("POST", "/api/order", {}, headers)[0], 400)

    def test_read_only_observation_still_rejects_every_post(self):
        self.handler = make_handler(LocalReadOnlyObservationService())
        self.assertEqual(self.call("POST", "/api/exit-all", {"request_id": str(uuid.uuid4())})[0], 403)

    def test_paper_protective_model_ui_has_only_an_emergency_mutation(self):
        fixture = protective_fixtures.ProtectiveExitTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.filled()
        fixture.protected()
        self.handler = make_handler(LocalProtectiveExitService(fixture.manager))
        headers = self.auth()
        payload = {"request_id": str(uuid.uuid4())}
        self.assertEqual(self.call("POST", "/api/execute", {"direction": "CALL", **payload}, headers)[0], 403)
        self.assertEqual(self.call("POST", "/api/exit-all", payload, headers)[0], 200)
        status = json.loads(self.call("GET", "/api/status")[1])
        self.assertFalse(status["ready"])
        self.assertTrue(status["protective_exit_model"]["emergency"])
        self.assertFalse(status["executable"])


if __name__ == "__main__":
    unittest.main()
