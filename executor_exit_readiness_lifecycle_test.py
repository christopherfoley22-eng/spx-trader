"""Readiness cannot bypass Phase 7 independent storage/authentication recovery."""
import sqlite3
import unittest
from unittest.mock import patch
import json
import executor_liquidation_lifecycle_test as fixtures
from executor_blind_presentation import dashboard_response
from mortificatio_models import ApplicationError


class ReadinessLifecycleTest(unittest.TestCase):
    for _name in ('setUp','cleanup_lifecycle','enroll','login','grant','authorize','wall','mono','verify',
                  'enter','engine','orders','fill','last','restart','configure','tick'):
        locals()[_name]=getattr(fixtures.AdaptiveLifecycleTest,_name)

    def test_125_contract_outage_restart_preserves_durable_state_and_disposable_cache(self):
        result=self.enter();self.configure(result)
        self.lifecycle.enable_exit_readiness(self.service_token,result['trade_id']);self.fill(result,25)
        with patch.object(self.security.store,'database',side_effect=sqlite3.OperationalError('foundation unavailable')):
            self.tick(result,6050,'peak');self.restart();self.tick(result,6047,'exit')
            engine=self.engine(result)
            self.assertIsNotNone(engine.exit_readiness)
            self.assertEqual(sum(engine.positions().values()),125)
            self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),125)
            self.assertTrue(self.lifecycle.checkpoint.read()['fenced'])
            self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])

    def test_dashboard_authority_cannot_configure_cache_and_blind_presenter_has_no_templates(self):
        result=self.enter();self.configure(result)
        with self.assertRaises(ApplicationError):self.lifecycle.enable_exit_readiness(self.session.token,result['trade_id'])
        self.lifecycle.enable_exit_readiness(self.service_token,result['trade_id']);self.fill(result,1)
        raw={'lifecycle':{'trades':[self.lifecycle._trade_status(result['trade_id'])]}}
        text=json.dumps(dashboard_response('/api/status',raw))
        for field in ('templates','premium_stops','favorable_spx_peak','source_timestamp','arrival','limit_price'):
            self.assertNotIn(field,text)
        self.security.logout(self.session.token,self.session.csrf_token)
        self.tick(result,6005,'activate');self.tick(result,6002,'reverse')
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)


if __name__=='__main__':unittest.main()
