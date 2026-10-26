"""Incremental hints remain subordinate to durable strategy and closing safety."""
from dataclasses import replace,FrozenInstanceError
from decimal import Decimal
import unittest
from unittest.mock import patch
import executor_liquidation_test as fixtures
from executor_exit_readiness import enable_readiness
from executor_liquidation import GlobalLiquidationCoordinator
from executor_portfolio import OfflinePortfolioLedger
from executor_portfolio_liquidity import fixture_from_payload
from executor_paper_types import PaperBrokerError
from simulation_evidence import SPXObservation,FeedStatus,EvidenceError


class ReadinessTest(unittest.TestCase):
    for _name in ('setUp','fresh','account','quotes','schedule','own','enable','intents'):
        locals()[_name]=getattr(fixtures.CoordinatorTest,_name)

    def ready(self):
        self.enable();self.cache=enable_readiness(self.ledger);return self.cache

    def test_immutable_templates_have_no_ids_grants_or_submission_authority(self):
        cache=self.ready();self.assertEqual(len(cache.templates),5)
        t=cache.templates[101]
        with self.assertRaises(FrozenInstanceError):t.uncommitted_quantity=1000
        self.assertEqual((t.side,t.order_type,t.uncommitted_quantity),('SELL','LMT',25))
        self.assertFalse(hasattr(t,'order_id'));self.assertFalse(hasattr(t,'token'))
        self.assertEqual(cache.premium_stops[101],('10','6.75'))

    def test_only_affected_strike_refreshes_on_quote_and_cold_cache_falls_back(self):
        cache=self.ready();before=cache.refreshes.copy();c=self.contracts[0]
        q=fixture_from_payload(self.ledger.portfolio['quotes'][str(c.con_id)])
        self.ledger.observe_liquidity(self.fake.generation,'new-quote',replace(q,quote_id='new-quote'))
        self.assertEqual(cache.refreshes[c.con_id],before[c.con_id]+1)
        self.assertEqual({k:v for k,v in cache.refreshes.items() if k!=c.con_id},{k:v for k,v in before.items() if k!=c.con_id})
        cache.invalidate();self.assertEqual(len(self.intents(self.schedule())),5);self.assertEqual(cache.misses,5)

    def test_warm_cache_selected_but_new_quotes_and_stale_data_cannot_authorize_dispatch(self):
        cache=self.ready();actions=self.intents(self.schedule());self.assertEqual(cache.hits,5)
        self.fake.advance(2)
        with self.assertRaises((PaperBrokerError,EvidenceError)):self.fake.dispatch(actions)
        self.assertTrue(all(self.ledger.state['intents'][a['intent_id']]['order_id'] is None for a in actions))

    def test_tampered_hint_cannot_override_original_floor(self):
        cache=self.ready()
        cache.templates[101]=replace(cache.templates[101],floor='1')
        actions=self.intents(self.schedule())
        with self.assertRaises(PaperBrokerError):self.fake.dispatch(actions)
        self.assertFalse(any(i['side']=='SELL' and i['order_id'] is not None for i in self.ledger.state['intents'].values()))

    def test_refresh_failure_is_disposable_not_rollback_of_committed_market_event(self):
        cache=self.ready();q=fixture_from_payload(self.ledger.portfolio['quotes']['101'])
        with patch.object(cache,'after_commit',side_effect=OSError('hint failure')):
            self.ledger.observe_liquidity(self.fake.generation,'committed',replace(q,quote_id='committed'))
        self.assertEqual(self.ledger.portfolio['quotes']['101']['quote_id'],'committed')
        self.assertFalse(cache.templates);self.assertTrue(cache.failure)
        self.assertTrue(self.intents(self.schedule()))

    def test_disconnect_and_restart_never_reuse_cached_authorization(self):
        self.ready();self.ledger.disconnect(self.fake.generation);self.assertFalse(self.cache.templates)
        old=self.ledger;old.close();self.ledgers.remove(old)
        self.ledger=OfflinePortfolioLedger(self.path,old.approval,fixtures.fixtures.policy())
        self.ledgers.append(self.ledger)
        self.assertIsNotNone(self.ledger.exit_readiness);self.assertFalse(self.ledger.exit_readiness.templates)
        with self.assertRaises(PaperBrokerError):self.schedule()

    def test_no_plan_is_order_intent_before_existing_strategy_exit(self):
        self.own();GlobalLiquidationCoordinator(self.ledger).configure(fixtures.AdaptiveExitPolicy('v'))
        cache=enable_readiness(self.ledger)
        self.assertEqual(len(cache.templates),5)
        self.assertFalse(any(i['side']=='SELL' for i in self.ledger.state['intents'].values()))
        before=cache.refreshes.copy()
        self.ledger.observe_spx(self.fake.generation,'arm',SPXObservation(6004,FeedStatus.LIVE,self.fake.wall,self.fake.mono))
        self.assertFalse(self.ledger.portfolio['exit_reason']);self.assertEqual(before,cache.refreshes)
        self.ledger.observe_spx(self.fake.generation,'trail',SPXObservation(6050,FeedStatus.LIVE,self.fake.wall,self.fake.mono))
        self.assertTrue(cache.strategy['trailing']);self.assertEqual(Decimal(cache.strategy['peak']),6050)
        self.ledger.observe_spx(self.fake.generation,'reverse',SPXObservation(6047,FeedStatus.LIVE,self.fake.wall,self.fake.mono))
        self.assertTrue(self.ledger.portfolio['exit_reason']);self.assertEqual(len(self.intents(self.schedule())),5)

    def test_sqlite_full_prevents_reservation_and_dispatch_even_with_warm_cache(self):
        self.ready()
        with patch.object(self.ledger,'_append',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.schedule()
        self.assertFalse(any(i['side']=='SELL' for i in self.ledger.state['intents'].values()))


if __name__=='__main__':unittest.main()
