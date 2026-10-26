"""Adaptive reductions retain Phase 1–7 authority/checkpoints and Blind Mode."""
from decimal import Decimal
from dataclasses import replace
import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch
import executor_lifecycle_test as fixtures
from executor_adaptive_liquidity import AdaptiveExitPolicy
from executor_blind_presentation import dashboard_response
from mortificatio_models import ApplicationError
from simulation_evidence import SPXObservation,FeedStatus


class AdaptiveLifecycleTest(unittest.TestCase):
    for _name in ('setUp','cleanup_lifecycle','enroll','login','grant','authorize','wall','mono','verify',
                  'enter','engine','orders','fill','last','restart'):
        locals()[_name]=getattr(fixtures.LifecycleTest,_name)

    def configure(self,result):
        self.lifecycle.configure_exit_execution(self.service_token,result['trade_id'],AdaptiveExitPolicy('offline-lifecycle-v1'))

    def tick(self,result,price,event):
        return self.lifecycle.observe_spx(self.service_token,result['trade_id'],event,
            SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))

    def test_foundation_outage_partial_entry_and_restart_preserve_peak_and_reduce_only(self):
        result=self.enter();self.configure(result);self.fill(result,1)
        with patch.object(self.security.store,'database',side_effect=sqlite3.OperationalError('foundation missing')):
            self.tick(result,6050,'peak');self.restart();self.tick(result,6047,'reversal')
            engine=self.engine(result)
            self.assertEqual(Decimal(engine.portfolio['peak']),6050)
            self.assertEqual(engine.portfolio['adaptive_exit']['policy']['version'],'offline-lifecycle-v1')
            self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
            self.assertTrue(self.lifecycle.checkpoint.read()['fenced'])
            self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])

    def test_revoked_dashboard_keeps_independent_protection_and_no_performance_disclosure(self):
        result=self.enter('PUT');self.configure(result);self.fill(result,1)
        self.security.logout(self.session.token,self.session.csrf_token)
        self.tick(result,5995,'activate');self.tick(result,5998,'reverse')
        engine=self.engine(result)
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
        raw={'lifecycle':{'trades':[self.lifecycle._trade_status(result['trade_id'])]}}
        public=json.dumps(dashboard_response('/api/status',raw))
        for secret in ('favorable_spx_peak','trailing_threshold','depth_evidence','arrival_price','limit_price','weighted_average_premium'):
            self.assertNotIn(secret,public)
        with self.assertRaises(ApplicationError):self.lifecycle.status(self.session.token)

    def test_optimizer_configuration_is_not_browser_authority(self):
        result=self.enter()
        with self.assertRaises(ApplicationError):
            self.lifecycle.configure_exit_execution(self.session.token,result['trade_id'],AdaptiveExitPolicy('unauthorized'))
        self.assertFalse(self.engine(result).portfolio.get('adaptive_exit'))

    def test_disconnect_never_authorizes_replacement_or_false_flat(self):
        result=self.enter();self.configure(result);self.fill(result,1)
        self.lifecycle.disconnect(self.service_token)
        self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])
        self.assertEqual(sum(self.engine(result).positions().values()),5)
        self.verify();self.tick(result,6004,'arm');self.tick(result,6001.25,'exit')
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)

    def test_repeated_emergency_late_duplicate_fill_and_reconciliation(self):
        result=self.enter();self.configure(result);self.fill(result,1)
        entry=self.orders(result)[0]
        for _ in range(2):
            self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        # An actual late broker fill is evidence, even after cancellation.
        for _ in range(2):self.fixture.fill(entry['order_ref'],1,'10',self.wall(),'adaptive-late.01')
        self.lifecycle.supervise(self.service_token)
        engine=self.engine(result)
        self.assertTrue(engine.portfolio['emergency'])
        self.assertEqual(sum(engine.positions().values()),6)
        for cid,owned in engine.positions().items():self.assertLessEqual(engine.reservations('SELL',cid),owned)
        self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])
        for _ in range(3):
            self.fill(result,side='SELL',price='10')
            self.lifecycle.supervise(self.service_token)
        self.assertTrue(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])

    def test_expired_session_premium_stop_uses_fresh_source_evidence_only(self):
        result=self.enter();self.configure(result);self.fill(result,1);self.now+=901
        self.lifecycle.supervise(self.service_token)
        self.assertFalse(self.orders(result,'SELL'))
        for q in self.provider.snapshot('CALL').quotes:
            if str(q.option.contract.con_id) not in self.engine(result).portfolio['targets']:continue
            key=str(uuid.uuid4())
            self.lifecycle.observe_liquidity(self.service_token,result['trade_id'],key,
                replace(q,quote_id=key,option=replace(q.option,bid=6.75,ask=6.85)))
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
        self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])

    def test_125_contract_foundation_outage_and_restart_retains_every_strike(self):
        result=self.enter();self.configure(result);self.fill(result,25)
        with patch.object(self.security.store,'database',side_effect=sqlite3.OperationalError('foundation unavailable')):
            self.tick(result,6050,'full-peak');self.restart();self.tick(result,6047,'full-exit')
            engine=self.engine(result)
            self.assertEqual(sum(engine.positions().values()),125)
            self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),125)
            self.assertTrue(all(engine.reservations('SELL',cid)<=25 for cid in engine.positions()))
            self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])


if __name__=='__main__':unittest.main()
