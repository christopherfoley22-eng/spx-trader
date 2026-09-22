"""Deterministic normalized and raw callback traces; no broker connection."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import unittest

from executor_market_ingestion import (
    IngestionError, OPTION_NORMALIZED_CALLBACK, PROVEN_SEMANTICS,
    ReadOnlyMarketIngestion, SPX_NORMALIZED_CALLBACK, SPXContract,
    VALIDATED_IBKR_LIMITATIONS,
)
from executor_ibkr_observation import ReadOnlyIBKREvidenceAdapter
from executor_redaction import safe_error_text
from simulation_evidence import (
    MAX_AGE_SECONDS, MAX_FUTURE_SECONDS, MAX_SYNC_SECONDS,
    VerifiedOptionContract,
)

NOW = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)
SPX = SPXContract("SPX", "IND", "USD", 101, "CBOE")
OPTION = VerifiedOptionContract("SPX", "OPT", "C", "20260922", "SPXW",
                                "100", "USD", 5000.0, 202)
OPAQUE_SECOND = 1790087400


class MarketIngestionTest(unittest.TestCase):
    def setUp(self):
        self.market = ReadOnlyMarketIngestion()
        self.generation = self.market.connect()
        self.market.register(self.generation, 10, "SPX_UNDERLYING_MARKET_DATA",
                             "SPX_UNDERLYING", SPX)
        self.market.register(self.generation, 11, "EXACT_SPXW_OPTION_MARKET_DATA",
                             "EXACT_SPXW_OPTION", OPTION)
        self.sequence = 0

    def event_id(self, prefix):
        self.sequence += 1
        return "%s-%d" % (prefix, self.sequence)

    def result(self, elapsed=0.1, now_wall=None, session=lambda _: True):
        return self.market.status(now_wall or NOW + timedelta(seconds=elapsed),
                                  100.0 + elapsed, "CALL", session)

    def live(self, binding=True):
        self.market.market_data_type(self.generation, 10, 1, NOW, 99.9, binding)
        self.market.market_data_type(self.generation, 11, 1, NOW, 99.9, binding)

    def spx(self, source=NOW, price=5000.0, receipt=100.0, receipt_wall=NOW,
            uncertainty=.01, event_id=None, contract=SPX, request_id=10,
            generation=None, semantics=PROVEN_SEMANTICS,
            callback=SPX_NORMALIZED_CALLBACK):
        self.market.proven_spx_event(
            self.generation if generation is None else generation, request_id, contract,
            event_id or self.event_id("spx"), source, uncertainty, price,
            receipt_wall, receipt, semantics, callback)

    def option(self, source=NOW, bid=10.0, ask=10.1, receipt=100.0,
               receipt_wall=NOW, uncertainty=.01, event_id=None, contract=OPTION,
               request_id=11, generation=None, callback=OPTION_NORMALIZED_CALLBACK,
               **metadata):
        self.market.proven_option_quote(
            self.generation if generation is None else generation, request_id, contract,
            event_id or self.event_id("option"), source, uncertainty, bid, ask,
            receipt_wall, receipt, PROVEN_SEMANTICS, callback, **metadata)

    def pair(self, **kwargs):
        self.live()
        self.spx(**kwargs)
        self.option(**kwargs)

    def assert_blocked(self, reason=None, **kwargs):
        result = self.result(**kwargs)
        self.assertFalse(result["source_pair_valid"])
        self.assertFalse(result["executable"])
        if reason:
            self.assertTrue(any(reason in item for item in result["block_reasons"]), result)

    def test_installed_callback_shapes_and_documented_limitations(self):
        wrapper = next((Path(__file__).parent / ".venv").glob(
            "lib/python*/site-packages/ibapi/wrapper.py"))
        tree = ast.parse(wrapper.read_text())
        ewrapper = next(node for node in tree.body
                        if isinstance(node, ast.ClassDef) and node.name == "EWrapper")
        functions = {node.name: node for node in ewrapper.body
                     if isinstance(node, ast.FunctionDef)}
        expected = {
            "marketDataType": ("reqId", "marketDataType"),
            "tickByTickAllLast": ("reqId", "tickType", "time", "price", "size",
                                  "tickAttribLast", "exchange", "specialConditions"),
            "tickByTickBidAsk": ("reqId", "time", "bidPrice", "askPrice",
                                 "bidSize", "askSize", "tickAttribBidAsk"),
            "tickByTickMidPoint": ("reqId", "time", "midPoint"),
        }
        for name, arguments in expected.items():
            self.assertEqual(tuple(arg.arg for arg in functions[name].args.args)[1:],
                             arguments)
        self.assertIn("notification occurs only", ast.get_docstring(functions["marketDataType"]))

    def test_proven_pair_is_valid_but_never_executable(self):
        self.pair()
        result = self.result()
        self.assertTrue(result["source_pair_valid"])
        self.assertFalse(result["executable"])
        self.assertEqual(result["authoritative_provenance_path"],
                         "executor_market_ingestion")

    def test_whole_second_callbacks_are_diagnostic_only(self):
        self.live()
        self.market.tick_by_tick_last(self.generation, 10, SPX, 1, OPAQUE_SECOND,
                                      5000.0, NOW, 100.0)
        self.market.tick_by_tick_bid_ask(self.generation, 11, OPTION, OPAQUE_SECOND,
                                         10.0, 10.1, NOW, 100.0)
        self.assert_blocked("SPX_SOURCE_UNAVAILABLE")
        text = json.dumps(self.result())
        self.assertIn("OPAQUE_IBKR_INTEGER_AND_EVENT_IDENTITY_AMBIGUOUS_DIAGNOSTIC_ONLY",
                      text)
        self.assertNotIn("1790087400", text)

    def test_precision_gate_exact_boundary_and_just_over(self):
        self.live()
        self.spx(uncertainty=.10)
        self.option(source=NOW + timedelta(seconds=.05), uncertainty=.10)
        self.assertTrue(self.result(now_wall=NOW + timedelta(seconds=.1))["source_pair_valid"])
        other = MarketIngestionTest("runTest")
        other.setUp()
        other.live()
        other.spx(uncertainty=.10)
        other.option(source=NOW + timedelta(seconds=.051), uncertainty=.10)
        other.assert_blocked("SOURCE_PRECISION_CANNOT_PROVE_SYNCHRONIZATION")

    def test_same_whole_second_never_proves_quarter_second_gate(self):
        self.live()
        self.market.tick_by_tick_last(self.generation, 10, SPX, 2, OPAQUE_SECOND,
                                      5000.0, NOW, 100.0)
        self.market.tick_by_tick_bid_ask(self.generation, 11, OPTION, OPAQUE_SECOND,
                                         10.0, 10.1, NOW, 100.0)
        self.assert_blocked("SPX_SOURCE_UNAVAILABLE")
        self.assertIn("TICK_BY_TICK_TIME_PRECISION_INSUFFICIENT_FOR_0_25_SECOND_GATE",
                      self.result()["runtime_dependencies"])

    def test_event_identity_allows_legitimate_same_time_updates(self):
        self.live()
        self.spx(event_id="s1", price=5000.0)
        self.spx(event_id="s2", price=5001.0, receipt=100.01)
        self.option(event_id="o1", bid=10.0, ask=10.1, receipt=100.01)
        self.option(event_id="o2", bid=10.2, ask=10.3, receipt=100.02)
        self.assertTrue(self.result()["source_pair_valid"])
        self.assertEqual(self.market.requests[10].spx.price, 5001.0)
        self.assertEqual(self.market.requests[11].option.bid, 10.2)

    def test_duplicate_event_is_idempotent_and_conflict_invalidates(self):
        self.live()
        self.spx(event_id="same")
        original = self.market.requests[10].spx.provenance.receipt_monotonic
        self.spx(event_id="same", receipt=100.1)
        self.assertEqual(self.market.requests[10].spx.provenance.receipt_monotonic, original)
        with self.assertRaises(IngestionError):
            self.spx(event_id="same", price=5001.0, receipt=100.2)
        self.assert_blocked("SPX_REQUEST_INVALID")

    def test_live_request_and_callback_without_binding_remain_unavailable(self):
        self.live(binding=False)
        self.spx()
        self.option()
        self.assert_blocked("SPX_LIVE_REQUEST_BINDING_UNPROVEN")
        self.assertFalse(self.result()["requests"]["SPX_UNDERLYING"]
                         ["classification_binding_proven"])

    def test_nonlive_classifications_never_pass(self):
        for code, label in ((2, "FROZEN"), (3, "DELAYED"), (4, "DELAYED_FROZEN")):
            case = MarketIngestionTest("runTest")
            case.setUp()
            case.market.market_data_type(case.generation, 10, code, NOW, 99.9, True)
            case.market.market_data_type(case.generation, 11, code, NOW, 99.9, True)
            case.spx()
            case.option()
            case.assert_blocked("SPX_" + label)

    def test_source_and_receipt_freshness_boundaries(self):
        self.pair()
        self.assertTrue(self.result(elapsed=MAX_AGE_SECONDS)["source_pair_valid"])
        self.assertFalse(self.result(elapsed=MAX_AGE_SECONDS + .001)["source_pair_valid"])
        future = MarketIngestionTest("runTest")
        future.setUp()
        future.live()
        future.spx(source=NOW + timedelta(seconds=MAX_FUTURE_SECONDS),
                   receipt_wall=NOW)
        with self.assertRaises(IngestionError):
            future.option(source=NOW + timedelta(seconds=MAX_FUTURE_SECONDS + .001),
                          receipt_wall=NOW)

    def test_bad_normalized_time_semantics_precision_and_rollback(self):
        self.live()
        bad = ((None, .01, PROVEN_SEMANTICS),
               (NOW, math.nan, PROVEN_SEMANTICS),
               (NOW, .01, "IBKR_OPAQUE_INTEGER"))
        for source, uncertainty, semantics in bad:
            case = MarketIngestionTest("runTest")
            case.setUp()
            case.live()
            with self.assertRaises(IngestionError):
                case.spx(source=source, uncertainty=uncertainty, semantics=semantics)
        self.spx(source=NOW + timedelta(milliseconds=10))
        with self.assertRaises(IngestionError):
            self.spx(source=NOW, receipt=100.1)

    def test_wrong_normalized_callback_role_and_malformed_update_invalidate(self):
        self.live()
        with self.assertRaises(IngestionError):
            self.spx(callback=OPTION_NORMALIZED_CALLBACK)
        self.assert_blocked("SPX_REQUEST_INVALID")
        other = MarketIngestionTest("runTest")
        other.setUp()
        other.pair()
        with self.assertRaises(IngestionError):
            other.option(bid=math.nan)
        other.assert_blocked("OPTION_REQUEST_INVALID")

    def test_request_generation_role_contract_and_identity_fail_closed(self):
        actions = (
            lambda case: case.spx(request_id=99),
            lambda case: case.spx(request_id=11),
            lambda case: case.spx(contract=replace(SPX, con_id=999)),
            lambda case: case.spx(generation=case.generation - 1),
            lambda case: case.market.register(case.generation, 10,
                                               "SPX_UNDERLYING_MARKET_DATA",
                                               "SPX_UNDERLYING", SPX),
        )
        for action in actions:
            case = MarketIngestionTest("runTest")
            case.setUp()
            with self.assertRaises(IngestionError):
                action(case)
            self.assertFalse(case.result()["source_pair_valid"])

    def test_option_contract_mismatches_fail_closed(self):
        for field, value in (("con_id", 999), ("expiration", "20260923"),
                             ("right", "P"), ("strike", 5005.0),
                             ("trading_class", "OTHER")):
            case = MarketIngestionTest("runTest")
            case.setUp()
            case.live()
            with self.assertRaises(IngestionError):
                case.option(contract=replace(OPTION, **{field: value}))

    def test_prices_crossed_locked_and_metadata_policy(self):
        self.live()
        for value in (0, -1, math.nan, math.inf, True):
            with self.assertRaises(IngestionError):
                self.option(bid=value)
        with self.assertRaises(IngestionError):
            self.option(bid=10.2, ask=10.1)
        locked = MarketIngestionTest("runTest")
        locked.setUp()
        locked.live()
        locked.spx()
        locked.option(bid=10.0, ask=10.0, bid_size=1, ask_size=2,
                      bid_past_low=True, ask_past_high=False)
        self.assertTrue(locked.result()["source_pair_valid"])
        quote = locked.market.requests[11].option
        self.assertEqual((quote.bid_size, quote.ask_size), (1, 2))

    def test_midpoint_last_alllast_and_standard_ticks_never_supply_source(self):
        self.live()
        self.market.tick_by_tick_midpoint(self.generation, 10, SPX, OPAQUE_SECOND,
                                          5000.0, NOW, 100.0)
        self.market.tick_by_tick_last(self.generation, 10, SPX, 1, OPAQUE_SECOND,
                                      5000.0, NOW, 100.01)
        self.market.tick_by_tick_last(self.generation, 10, SPX, 2, OPAQUE_SECOND,
                                      5000.0, NOW, 100.02)
        self.market.ordinary_tick(self.generation, 11, OPTION, "TICK_PRICE", 1,
                                  NOW, 100.03)
        self.market.ordinary_tick(self.generation, 11, OPTION, "TICK_STRING", 45,
                                  NOW, 100.04)
        result = self.result()
        self.assertFalse(result["source_pair_valid"])
        diagnostics = result["requests"]["SPX_UNDERLYING"]["callback_diagnostics"]
        self.assertEqual({item[0] for item in diagnostics},
                         {"TICK_BY_TICK_MIDPOINT", "TICK_BY_TICK_LAST",
                          "TICK_BY_TICK_ALL_LAST"})

    def test_provenance_status_is_separated_and_sanitized(self):
        self.pair()
        result = self.result()
        provenance = result["requests"]["SPX_UNDERLYING"]["provenance"]
        self.assertEqual(provenance["callback_type"], SPX_NORMALIZED_CALLBACK)
        self.assertEqual(provenance["source_semantics"], PROVEN_SEMANTICS)
        self.assertIn("normalized_source_time_utc", provenance)
        self.assertIn("wall_receipt_time_utc", provenance)
        self.assertIn("monotonic_receipt", provenance)
        rendered = json.dumps(result).lower()
        self.assertNotIn("con_id", rendered)
        self.assertNotIn("exchange_time", rendered)
        self.assertNotIn('"price"', rendered)

    def test_request_and_global_informational_messages_are_diagnostic_only(self):
        self.pair()
        self.market.error(self.generation, -1, 2104, "farm OK", receipt_utc=NOW,
                          receipt_monotonic=100.0)
        self.market.error(self.generation, 11, 2106, "farm connected",
                          receipt_utc=NOW, receipt_monotonic=100.0)
        result = self.result()
        self.assertTrue(result["source_pair_valid"])
        self.assertTrue(all(item["informational"] for item in result["errors"]))
        self.assertFalse(result["executable"])

    def test_unknown_and_request_errors_invalidate_scope(self):
        self.pair()
        self.market.error(self.generation, 11, 10168, "not subscribed",
                          receipt_utc=NOW, receipt_monotonic=100.0)
        self.assert_blocked("OPTION_REQUEST_INVALID")
        other = MarketIngestionTest("runTest")
        other.setUp()
        other.market.error(other.generation, -1, 99999, "unknown",
                           receipt_utc=NOW, receipt_monotonic=100.0)
        other.assert_blocked("GLOBAL_OR_UNKNOWN_ERROR")

    def test_robust_error_redaction(self):
        account = "DU" + "1234567"
        message = ('{"account":"%s","token":"abc","balance":12345,'
                   '"note":"person@example.test"}') % account
        cleaned = safe_error_text(message)
        for secret in (account, "abc", "12345", "person@example.test"):
            self.assertNotIn(secret, cleaned)
        synthetic_value = "hunter" + "2"
        self.assertNotIn(synthetic_value,
                         safe_error_text(("pass" + "word") + "=" + synthetic_value))
        self.assertEqual(safe_error_text(None), "[NON_TEXT_ERROR]")
        encoded = "SPXW  " + "260922C07195000"
        human = "SPX (SPXW) " + "SEP 22 '26 7195 Call"
        for value in (encoded, human, "strike=7195 expiration=20260922 conId=123456"):
            cleaned = safe_error_text("request failed for " + value)
            self.assertNotIn("7195", cleaned)
            self.assertNotIn("20260922", cleaned)
            self.assertNotIn("123456", cleaned)

    def test_session_validator_exception_is_contained(self):
        self.pair()
        def broken(_):
            raise RuntimeError("sensitive internal detail")
        result = self.result(session=broken)
        self.assertFalse(result["source_pair_valid"])
        self.assertIn("PAIR_PROVENANCE_INVALID: SESSION_VALIDATOR_FAILURE",
                      result["block_reasons"])
        self.assertNotIn("sensitive", json.dumps(result))

    def test_cancel_disconnect_reconnect_and_old_callbacks(self):
        self.pair()
        self.market.cancel(self.generation, 11)
        self.assert_blocked("OPTION_REQUEST_INVALID")
        self.market.disconnect(self.generation)
        self.assert_blocked("DISCONNECTED")
        new_generation = self.market.connect()
        self.assertGreater(new_generation, self.generation)
        with self.assertRaises(IngestionError):
            self.market.market_data_type(self.generation, 10, 1, NOW, 100.0, True)

    def test_legacy_adapter_cannot_contribute_market_readiness(self):
        adapter = ReadOnlyIBKREvidenceAdapter()
        source = Path(__file__).with_name("executor_ibkr_observation.py").read_text()
        self.assertIn("AUTHORITATIVE MARKET INGESTION REDUCER REQUIRED", source)
        self.assertFalse(adapter.observe(NOW, 100.0).executable)

    def test_shared_policy_and_runtime_dependencies(self):
        source = Path(__file__).with_name("executor_market_ingestion.py").read_text()
        tree = ast.parse(source)
        imported = next(node for node in tree.body if isinstance(node, ast.ImportFrom)
                        and node.module == "simulation_evidence")
        names = {alias.name for alias in imported.names}
        self.assertTrue({"MAX_AGE_SECONDS", "MAX_FUTURE_SECONDS", "MAX_SYNC_SECONDS",
                         "EvidenceClock"} <= names)
        result = self.result()
        for dependency in ("SPX_INDEX_CALLBACK_STREAM_UNPROVEN",
                           "TICK_BY_TICK_MIDPOINT_RELEVANCE_UNPROVEN",
                           "IBKR_INTEGER_TIME_SEMANTICS_UNPROVEN",
                           "TICK_BY_TICK_MARKET_DATA_TYPE_BINDING_UNPROVEN"):
            self.assertIn(dependency, result["runtime_dependencies"])
        for limitation in VALIDATED_IBKR_LIMITATIONS:
            self.assertIn(limitation, result["runtime_dependencies"])

    def test_no_broker_import_or_mutation_reachability(self):
        path = Path(__file__).with_name("executor_market_ingestion.py")
        tree = ast.parse(path.read_text())
        self.assertFalse(any(isinstance(node, (ast.Import, ast.ImportFrom)) and
                             any(alias.name.startswith("ibapi") for alias in node.names)
                             for node in ast.walk(tree)))
        forbidden = {"placeOrder", "cancelOrder", "exerciseOptions", "submit",
                     "connect_ibkr", "reqGlobalCancel"}
        self.assertFalse(any(hasattr(ReadOnlyMarketIngestion, name) for name in forbidden))


if __name__ == "__main__":
    unittest.main()
