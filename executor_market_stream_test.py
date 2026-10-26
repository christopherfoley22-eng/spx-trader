"""Offline Stage 2 callbacks, provenance, ranking, and reconnect attacks."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from executor_ibkr_observation import ReadOnlyIBKREvidenceAdapter, SIZING_TAGS
from executor_market_ingestion import IngestionError, OptionGreeks
from executor_market_stream import IBKRMarketCallbacks, ReadOnlyMarketStream
from executor_live_observation_service import LocalReadOnlyObservationService
from simulation_evidence import EvidenceError, VerifiedAccountSnapshot, VerifiedOptionContract

NOW = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)
ACCOUNT = "SYNTHETIC_PAPER_ONLY"


def greeks(delta=.48, **changes):
    return replace(OptionGreeks(delta, .01, .2, -.5, .25, 10.0, 5000.0, 0.0), **changes)


class MarketStreamTest(unittest.TestCase):
    def setup_stream(self, direction="CALL", strikes=(4995.0, 5000.0, 5005.0), offset=0):
        self.direction = direction
        self.adapter = ReadOnlyIBKREvidenceAdapter(ACCOUNT)
        generation = self.adapter.generation
        self.adapter.handshake(generation)
        self.adapter.managed_accounts(generation, (ACCOUNT,))
        self.adapter.spx_details(generation, "SPX", "IND", "USD", 100, "CBOE")
        self.adapter.spx_details_end(generation)
        self.adapter.chain(generation, "SMART", 100, "SPXW", "100", {"20260922"}, set(strikes))
        self.adapter.chain_end(generation)
        self.contracts = [VerifiedOptionContract("SPX", "OPT", "C" if direction == "CALL" else "P",
                          "20260922", "SPXW", "100", "USD", strike, 200 + index)
                          for index, strike in enumerate(strikes)]
        for contract in self.contracts:
            self.adapter.exact_option(generation, str(contract.con_id), contract, "SMART")
        self.adapter.exact_option_end(generation)
        self.stream = ReadOnlyMarketStream(self.adapter)
        self.generation = self.stream.begin_session(direction, NOW)
        self.spx_id = 10 + offset
        self.stream.register_spx(self.generation, self.spx_id)
        self.ids = []
        for index, contract in enumerate(self.contracts):
            request_id = 20 + offset + index
            self.stream.register_option(self.generation, request_id, contract)
            self.ids.append(request_id)
        self.bridge = IBKRMarketCallbacks(self.stream, self.generation, lambda: NOW, lambda: 100.0)
        self.account = VerifiedAccountSnapshot(ACCOUNT, ACCOUNT, True, "USD",
                                               "100000", "100000", "100000", "100000", "100000", 100.0)

    def rank(self, elapsed=.1, **changes):
        return self.stream.rank(self.generation, NOW + timedelta(seconds=elapsed),
                                100.0 + elapsed, changes.pop("account", self.account), **changes)

    def raw_stream(self):
        self.bridge.marketDataType(self.spx_id, 1)
        self.bridge.tickPrice(self.spx_id, 4, 5000.0, None)
        for index, request_id in enumerate(self.ids):
            self.bridge.marketDataType(request_id, 1)
            self.bridge.tickPrice(request_id, 1, 9.9, None)
            self.bridge.tickPrice(request_id, 2, 10.0, None)
            self.bridge.tickOptionComputation(request_id, 13, 0, .25,
                                              .48 if self.direction == "CALL" else -.48,
                                              10.0, 0.0, .01, .2, -.5, 5000.0)

    def prove(self, deltas=None, greek_time=NOW, greek_receipt=100.0, greek_uncertainty=.01):
        """Explicit synthetic proof, never a path taken by the inbound bridge."""
        for index, (request_id, pair) in enumerate(self.stream.pairs.items()):
            generation = pair.generation
            contract = self.stream.requests[request_id]["contract"]
            spx = self.stream.requests[self.spx_id]["contract"]
            pair.market_data_type(generation, self.spx_id, 1, NOW, 100.0, True)
            pair.market_data_type(generation, request_id, 1, NOW, 100.0, True)
            pair.proven_spx_event(generation, self.spx_id, spx, "spx", NOW, .01, 5000.0, NOW, 100.0)
            pair.proven_option_quote(generation, request_id, contract, "quote", NOW,
                                      .01, 9.9, 10.0, NOW, 100.0)
            delta = (deltas or (.50, .47, .48))[index]
            if self.direction == "PUT":
                delta = -abs(delta)
            pair.proven_option_greeks(generation, request_id, contract, "greeks",
                                       greek_time, greek_uncertainty, greeks(delta),
                                       NOW, greek_receipt)

    def test_raw_stream_collects_quotes_and_all_greeks_but_never_proves_source(self):
        self.setup_stream()
        with patch("socket.socket", side_effect=AssertionError("No sockets allowed")):
            self.raw_stream()
            rows = self.stream.diagnostics(self.generation, NOW, 100.0)
            self.assertTrue(all(row["quote_locally_usable"] for row in rows))
            self.assertEqual(rows[1]["greeks"]["values"]["delta"], .48)
            self.assertEqual(rows[1]["greeks"]["values"]["theta"], -.5)
            for row in rows:
                self.assertFalse(row["source_proven"])
                self.assertFalse(row["executable"])
                for value in row["values"].values():
                    self.assertIsNone(value["exchange_time"])
                    self.assertIsNone(value["source_time"])
                    self.assertEqual(value["monotonic_receipt"], 100.0)
            result = self.rank()
            self.assertIsNone(result["selected"])
            self.assertFalse(result["executable"])
            self.assertIn("UNPROVEN", str(result["block_reasons"]))

    def test_synthetic_proven_atm_delta_ranking_and_25_cap_for_both_rights(self):
        for direction in ("CALL", "PUT"):
            with self.subTest(direction=direction):
                self.setup_stream(direction)
                self.prove()
                result = self.rank()
                self.assertEqual(result["block_reasons"], [])
                self.assertFalse(result["executable"])
                self.assertEqual(result["selected"]["strike"], 5005)
                self.assertEqual(result["selected"]["quantity"], 25)
                self.assertEqual(abs(result["selected"]["delta"]), .48)
                self.assertEqual(result["selected"]["expiration"], "20260922")

    def test_delta_ties_prefer_atm_then_lower_strike(self):
        self.setup_stream()
        self.prove(deltas=(.49, .49, .49))
        self.assertEqual([row["strike"] for row in self.rank()["ranked_candidates"]], [5000, 4995, 5005])

    def test_farther_delta_match_is_excluded_from_atm_universe(self):
        self.setup_stream(strikes=(4990., 4995., 5000., 5005., 5010.))
        self.prove(deltas=(.48, .49, .50, .51, .48))
        self.assertEqual({row["strike"] for row in self.rank()["ranked_candidates"]}, {4995, 5000, 5005})

    def test_affordability_and_stale_wrong_account_sizing(self):
        self.setup_stream()
        self.prove()
        low = replace(self.account, settled_cash="1500")
        self.assertEqual(self.rank(account=low)["selected"]["quantity"], 1)
        for account in (replace(low, settled_cash="999"), replace(low, complete=False),
                        replace(low, account_id="OTHER"),
                        replace(low, oldest_required_receipt_monotonic=98)):
            self.assertIsNone(self.rank(account=account)["selected"])

    def test_missing_or_stale_proven_greeks_blocks_ranking(self):
        self.setup_stream()
        self.prove()
        pair = self.stream.pairs[self.ids[0]]
        pair.requests[self.ids[0]] = replace(pair.requests[self.ids[0]], greeks=None)
        self.assertIn("GREEKS_SOURCE_UNAVAILABLE", str(self.rank()["block_reasons"]))
        self.setup_stream()
        self.prove()
        self.assertIsNone(self.rank(elapsed=1.01)["selected"])

    def test_greeks_source_and_receipt_synchronization_gate(self):
        for kwargs in (dict(greek_time=NOW - timedelta(seconds=.251)),
                       dict(greek_receipt=100.251), dict(greek_uncertainty=.25)):
            self.setup_stream()
            self.prove(**kwargs)
            result = self.rank(elapsed=.3)
            self.assertIsNone(result["selected"])
            self.assertIn("GREEKS_SYNCHRONIZATION_UNPROVEN", str(result["block_reasons"]))

    def test_delayed_frozen_unclassified_and_delayed_tick_types_are_rejected(self):
        for code in (2, 3, 4, 0, True):
            self.setup_stream()
            self.bridge.marketDataType(self.ids[0], code)
            with self.assertRaises(EvidenceError):
                self.bridge.tickPrice(self.ids[0], 1, 9.9, None)
            self.assertIsNone(self.rank()["selected"])
        for tick in (66, 67, 68):
            self.setup_stream()
            self.raw_stream()
            with self.assertRaises(EvidenceError):
                self.bridge.tickPrice(self.ids[0], tick, 9.9, None)
            self.assertIsNone(self.rank()["selected"])

    def test_incomplete_crossed_stale_or_unsynchronized_raw_quotes(self):
        self.setup_stream()
        self.bridge.marketDataType(self.ids[0], 1)
        self.bridge.tickPrice(self.ids[0], 1, 9.9, None)
        self.assertFalse(self.stream.diagnostics(self.generation, NOW, 100.)[1]["quote_locally_usable"])
        self.raw_stream()
        self.bridge.tickPrice(self.ids[0], 1, 10.1, None)
        self.assertIn("CROSSED_QUOTE", self.stream.diagnostics(self.generation, NOW, 100.)[1]["block_reasons"])
        self.assertFalse(self.stream.diagnostics(self.generation, NOW+timedelta(seconds=2), 102.)[1]["quote_locally_usable"])
        self.setup_stream()
        self.raw_stream()
        self.stream.tick_price(self.generation, self.ids[0], 1, 9.9, NOW, 100.3)
        self.assertIn("LOCAL_RECEIPTS_NOT_SYNCHRONIZED",
                      self.stream.diagnostics(self.generation, NOW+timedelta(seconds=.3), 100.3)[1]["block_reasons"])

    def test_missing_greeks_sentinels_and_wrong_right_delta(self):
        for value in (None, greeks(delta=None), greeks(delta=True), greeks(delta=2),
                      greeks(delta=-.48), greeks(delta=float("nan")),
                      greeks(gamma=-2), greeks(vega=1.7976931348623157e308),
                      greeks(underlying_price=0), greeks(implied_volatility=-1)):
            self.setup_stream()
            self.raw_stream()
            with self.assertRaises(EvidenceError):
                self.stream.option_computation(self.generation, self.ids[0], 13, value, NOW, 100.)
            self.assertIsNone(self.stream.requests[self.ids[0]]["greeks"])
            self.assertIsNone(self.rank()["selected"])
        for tick in (80, 81, 82, 83):
            self.setup_stream()
            self.raw_stream()
            with self.assertRaises(EvidenceError):
                self.stream.option_computation(self.generation, self.ids[0], tick, greeks(), NOW, 100.)
        self.setup_stream()
        self.raw_stream()
        for tick in (10, 11, 12):
            self.stream.option_computation(self.generation, self.ids[0], tick, greeks(.9), NOW, 100.)
        self.assertEqual(self.stream.requests[self.ids[0]]["greeks"]["values"]["delta"], .48)

    def test_wrong_contracts_and_expirations_are_not_registered(self):
        self.setup_stream()
        contract = self.contracts[0]
        for change in (dict(con_id=999), dict(right="P"), dict(symbol="SPY"),
                       dict(trading_class="SPX"), dict(multiplier="10"),
                       dict(expiration="20260923"), dict(strike=5010)):
            with self.assertRaises(EvidenceError):
                self.stream.register_option(self.generation, 999, replace(contract, **change))
        with self.assertRaises(EvidenceError):
            self.stream.begin_session("PUT", NOW)

    def test_no_current_day_expiration_and_ambiguous_contracts_fail_closed(self):
        self.setup_stream()
        self.stream.end_session(self.generation)
        with self.assertRaises(EvidenceError):
            self.stream.begin_session("CALL", NOW + timedelta(days=1))
        self.adapter.exact["ambiguous"] = (replace(self.contracts[0], con_id=999), "SMART")
        with self.assertRaises(EvidenceError):
            self.stream.begin_session("CALL", NOW)

    def test_new_york_expiration_and_cross_right_conid_contradictions(self):
        self.setup_stream()
        next_utc_date = datetime(2026, 9, 23, 0, 30, tzinfo=timezone.utc)
        contracts = self.adapter.current_spxw_contracts(self.adapter.generation, "CALL", next_utc_date)
        self.assertTrue(all(contract.expiration == "20260922" for contract in contracts))
        self.adapter.exact["wrong-right-same-id"] = (replace(self.contracts[0], right="P"), "SMART")
        with self.assertRaises(EvidenceError):
            self.adapter.current_spxw_contracts(self.adapter.generation, "CALL", NOW)

    def test_happy_reconnect_requires_new_ids_quotes_and_proof(self):
        self.setup_stream()
        self.prove()
        self.assertIsNotNone(self.rank()["selected"])
        self.stream.end_session(self.generation)
        self.generation = self.stream.begin_session("CALL", NOW)
        self.spx_id = 110
        self.ids = [120, 121, 122]
        self.stream.register_spx(self.generation, self.spx_id)
        for request_id, contract in zip(self.ids, self.contracts):
            self.stream.register_option(self.generation, request_id, contract)
        self.assertIsNone(self.rank()["selected"])
        self.bridge = IBKRMarketCallbacks(self.stream, self.generation, lambda: NOW, lambda: 100.)
        self.raw_stream()
        self.assertIsNone(self.rank()["selected"])
        self.prove()
        self.assertIsNotNone(self.rank()["selected"])

    def test_missing_atm_candidate_blocks_instead_of_falling_farther_out(self):
        self.setup_stream()
        self.prove()
        del self.stream.pairs[self.ids[0]]
        self.assertIn("ATM_CANDIDATE_UNIVERSE_INCOMPLETE", str(self.rank()["block_reasons"]))

    def test_feed_error_invalidates_proof_and_reconnect_clears_all_quotes(self):
        self.setup_stream()
        self.prove()
        self.bridge.error(self.ids[0], 354, "Requested market data not subscribed")
        self.assertIsNone(self.rank()["selected"])
        old_bridge = self.bridge
        self.stream.end_session(self.generation)
        self.generation = self.stream.begin_session("CALL", NOW)
        self.assertEqual(self.stream.requests, {})
        with self.assertRaises(EvidenceError):
            self.stream.register_spx(self.generation, self.spx_id)
        self.spx_id = 110
        self.stream.register_spx(self.generation, self.spx_id)
        for index, contract in enumerate(self.contracts):
            self.stream.register_option(self.generation, 120 + index, contract)
        with self.assertRaises(EvidenceError):
            old_bridge.tickPrice(20, 1, 10., None)
        self.assertIsNone(self.rank()["selected"])
        self.stream.end_session(self.generation)  # A failed session must still be closable.

    def test_adapter_reset_unknown_request_and_clock_rollback_invalidate_session(self):
        self.setup_stream()
        self.adapter.reset()
        with self.assertRaises(EvidenceError):
            self.bridge.marketDataType(self.spx_id, 1)
        self.assertIsNone(self.rank()["selected"])
        self.stream.end_session(self.generation)
        self.setup_stream()
        with self.assertRaises(EvidenceError):
            self.bridge.tickPrice(999, 1, 10., None)
        self.setup_stream()
        self.raw_stream()
        with self.assertRaises(EvidenceError):
            self.stream.tick_price(self.generation, self.ids[0], 1, 9.9, NOW, 99.)

    def test_frozen_transition_clears_quotes_and_greeks(self):
        self.setup_stream()
        self.raw_stream()
        self.bridge.marketDataType(self.ids[0], 2)
        self.bridge.marketDataType(self.ids[0], 1)
        self.assertEqual(self.stream.requests[self.ids[0]]["values"], {})
        self.assertIsNone(self.stream.requests[self.ids[0]]["greeks"])
        self.assertIsNone(self.rank()["selected"])

    def test_raw_callback_cannot_leave_older_proof_usable(self):
        self.setup_stream()
        self.raw_stream()
        self.prove()
        self.assertIsNotNone(self.rank()["selected"])
        self.bridge.tickPrice(self.ids[0], 1, 9.8, None)
        self.assertIsNone(self.rank()["selected"])
        self.setup_stream()
        self.raw_stream()
        self.prove()
        self.stream.option_computation(self.generation, self.ids[0], 13, greeks(), NOW, 100.)
        self.assertIsNone(self.rank()["selected"])

    def test_proven_greeks_duplicates_conflicts_and_wrong_identity(self):
        self.setup_stream()
        self.prove()
        pair = self.stream.pairs[self.ids[0]]
        request_id, contract = self.ids[0], self.contracts[0]
        pair.proven_option_greeks(pair.generation, request_id, contract, "greeks", NOW, .01,
                                   greeks(.5), NOW, 100.)
        self.assertIsNotNone(self.rank()["selected"])
        with self.assertRaises(IngestionError):
            pair.proven_option_greeks(pair.generation, request_id, contract, "greeks", NOW, .01,
                                       greeks(.51), NOW, 100.)
        self.assertIsNone(self.rank()["selected"])
        self.setup_stream()
        self.prove()
        pair = self.stream.pairs[self.ids[0]]
        with self.assertRaises(IngestionError):
            pair.proven_option_greeks(pair.generation, self.ids[0], replace(self.contracts[0], con_id=999),
                                       "wrong", NOW, .01, greeks(.5), NOW, 100.)
        self.assertIsNone(self.rank()["selected"])

    def test_installed_sdk_callback_shapes_and_manifest_have_no_execution(self):
        self.setup_stream()
        wrapper = next((Path(__file__).parent / ".venv").glob("lib/python*/site-packages/ibapi/wrapper.py"))
        installed = next(node for node in ast.parse(wrapper.read_text()).body
                         if isinstance(node, ast.ClassDef) and node.name == "EWrapper")
        source = ast.parse(Path(__file__).with_name("executor_market_stream.py").read_text())
        bridge = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "IBKRMarketCallbacks")
        for name in ("tickOptionComputation", "tickPrice", "marketDataType"):
            expected = next(node for node in installed.body if isinstance(node, ast.FunctionDef) and node.name == name)
            actual = next(node for node in bridge.body if isinstance(node, ast.FunctionDef) and node.name == name)
            self.assertEqual([arg.arg for arg in actual.args.args], [arg.arg for arg in expected.args.args])
        for api in ("connect", "placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions", "submit", "execute"):
            self.assertFalse(hasattr(self.bridge, api))
            self.assertFalse(hasattr(self.stream, api))
        manifest = self.stream.subscription_manifest(self.generation)
        self.assertEqual(len(manifest), 4)
        self.assertTrue(all(row["api"] == "reqMktData" and not row["snapshot"]
                            and row["requested_market_data_type"] == 1 for row in manifest))

    def test_readonly_service_diagnostics_are_json_safe_and_do_not_enable_readiness(self):
        self.setup_stream()
        self.raw_stream()
        service = LocalReadOnlyObservationService(adapter=self.adapter, market_stream=self.stream)
        result = service.status()
        json.dumps(result, allow_nan=False)
        self.assertFalse(result["ready"])
        self.assertFalse(result["market_data"]["executable"])
        self.assertIsNone(result["market_data"]["selected"])
        result["market_callback_diagnostics"][1]["greeks"]["values"]["delta"] = .01
        self.assertEqual(self.stream.requests[self.ids[0]]["greeks"]["values"]["delta"], .48)

    def test_continuous_raw_callbacks_have_bounded_diagnostics(self):
        self.setup_stream()
        self.raw_stream()
        for _ in range(300):
            self.bridge.tickPrice(self.ids[0], 1, 9.9, None)
        self.assertLessEqual(len(self.stream.pairs[self.ids[0]].requests[self.ids[0]].diagnostics), 256)
        self.assertIsNone(self.rank()["selected"])

    def test_stale_greeks_with_fresh_quote_and_account_remain_blocked(self):
        self.setup_stream()
        self.prove()
        wall = NOW + timedelta(seconds=1.1)
        for request_id, pair in self.stream.pairs.items():
            pair.proven_spx_event(pair.generation, self.spx_id, pair.requests[self.spx_id].contract,
                                  "spx-new", wall, .01, 5000., wall, 101.1)
            pair.proven_option_quote(pair.generation, request_id, pair.requests[request_id].contract,
                                      "option-new", wall, .01, 9.9, 10., wall, 101.1)
        result = self.rank(elapsed=1.15,
                           account=replace(self.account, oldest_required_receipt_monotonic=101.1))
        self.assertIsNone(result["selected"])
        self.assertIn("stale", str(result["block_reasons"]).lower())


if __name__ == "__main__":
    unittest.main()
