"""Foundation outages, protective storage faults and concurrent recovery. Offline."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3
import subprocess
import sys
import json
import time
import unittest
import uuid
from unittest.mock import patch

import executor_lifecycle_test as fixtures
from mortificatio_models import ApplicationError,TradeCommand
from simulation_evidence import SPXObservation,FeedStatus


class ReliabilityTest(unittest.TestCase):
    for _name in ('setUp','cleanup_lifecycle','enroll','login','grant','authorize','wall','mono','verify',
                  'enter','engine','orders','fill','last','restart'):
        locals()[_name]=getattr(fixtures.LifecycleTest,_name)

    def tick(self,result,price,event=None):
        return self.lifecycle.observe_spx(self.service_token,result['trade_id'],event or str(uuid.uuid4()),
            SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))

    def outage(self):return patch.object(self.security.store,'database',side_effect=sqlite3.OperationalError('injected foundation outage'))

    def assert_owned_safe(self,result,quantity):
        engine=self.engine(result)
        self.assertEqual(sum(engine.positions().values()),quantity)
        for cid,owned in engine.positions().items():self.assertLessEqual(engine.reservations('SELL',cid),owned)
        self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])

    def test_foundation_outage_tracks_peak_and_dispatches_only_evidenced_reductions(self):
        result=self.enter();self.fill(result,1)
        with self.outage():
            self.tick(result,6005);self.tick(result,6050);status=self.tick(result,6047)
            self.assertTrue(status['entry_fenced']);self.assertFalse(status['foundation_available'])
            self.assertEqual(Decimal(self.engine(result).portfolio['peak']),6050)
            self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
            self.assert_owned_safe(result,5)
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT state FROM allocation_reservations').fetchone()[0],'HELD')
        self.assertTrue(self.lifecycle.checkpoint.read()['fenced'])
        with self.assertRaises(ApplicationError):
            self.lifecycle.direction_command(self.session.token,self.session.csrf_token,TradeCommand(str(uuid.uuid4()),'PUT',1))

    def test_application_write_lock_does_not_block_essential_risk_cycle(self):
        result=self.enter();self.fill(result,1)
        with self.security.store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            before=time.monotonic();self.tick(result,6005);self.tick(result,6002)
            self.assertLess(time.monotonic()-before,.75)
            self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
        self.assert_owned_safe(result,5)

    def test_partial_entry_premium_stop_continues_without_foundation_transactions(self):
        result=self.enter('PUT');self.fill(result,1)
        quote=self.provider.snapshot('PUT').quotes[4]
        quote=replace(quote,quote_id='outage-premium',option=replace(quote.option,bid=6.75,ask=6.85))
        with patch.object(self.security.store,'transact',side_effect=sqlite3.OperationalError('foundation transaction unavailable')):
            self.lifecycle.observe_liquidity(self.service_token,result['trade_id'],'premium-outage',quote)
            self.assertEqual(self.engine(result).portfolio['exit_reason'],'INITIAL_STOP')
            self.assert_owned_safe(result,5)

    def test_restart_with_foundation_offline_restores_catalog_peak_and_no_entry_replay(self):
        result=self.enter();self.fill(result,1);self.tick(result,6020)
        refs=[i['order_ref'] for i in self.orders(result)]
        with self.outage():
            self.restart();self.tick(result,6017)
            self.assertEqual(Decimal(self.engine(result).portfolio['peak']),6020)
            self.assertTrue(self.engine(result).portfolio['ride'])
            self.assertEqual([i['order_ref'] for i in self.orders(result)],refs)
            self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
            self.assert_owned_safe(result,5)

    def test_callback_cursor_commit_failure_replays_without_duplicate_economic_fill(self):
        result=self.enter();item=self.orders(result)[0]
        self.fixture.fill(item['order_ref'],1,'10',self.wall(),'cursor-crash.01')
        cursor=self.lifecycle.checkpoint.read()['callback_cursor']
        with patch.object(self.lifecycle.checkpoint,'cursor',side_effect=OSError('checkpoint disk full')):
            with self.assertRaises(OSError):self.lifecycle.supervise(self.service_token)
        self.assertEqual(self.lifecycle.checkpoint.read()['callback_cursor'],cursor)
        self.restart();self.lifecycle.supervise(self.service_token)
        self.assert_owned_safe(result,1)
        active=[e for e in self.engine(result).state['executions'].values() if e['active']]
        self.assertEqual(len(active),1)
        self.assertEqual(self.lifecycle.protective_health(self.service_token)['condition'],'CRITICAL_UNRESOLVED_PROTECTION')

    def test_protective_journal_disk_error_enters_critical_and_never_claims_liquidation(self):
        result=self.enter();self.fill(result,1);engine=self.engine(result)
        with patch.object(engine,'_append',side_effect=sqlite3.OperationalError('protective disk full')):
            with self.assertRaises(sqlite3.OperationalError):self.tick(result,6005)
        health=self.lifecycle.protective_health(self.service_token)
        self.assertEqual(health['condition'],'CRITICAL_UNRESOLVED_PROTECTION')
        self.assertTrue(health['durable_checkpoint_verified'])
        self.assert_owned_safe(result,5)
        self.restart()
        self.assertEqual(self.lifecycle.protective_health(self.service_token)['condition'],'CRITICAL_UNRESOLVED_PROTECTION')

    def test_checkpoint_storage_failure_uses_no_volatile_order_fallback(self):
        result=self.enter();self.fill(result,1)
        with patch.object(self.lifecycle.checkpoint.store,'database',side_effect=sqlite3.OperationalError('checkpoint unavailable')):
            with self.assertRaises(sqlite3.OperationalError):self.lifecycle.supervise(self.service_token)
            health=self.lifecycle.protective_health(self.service_token)
            self.assertFalse(health['durable_checkpoint_verified']);self.assertTrue(health['entry_fenced'])
        self.assertTrue(self.authority.emergency.blocked())
        self.assert_owned_safe(result,5)

    def test_concurrent_capability_emergencies_and_strategy_exit_during_foundation_outage(self):
        result=self.enter();self.fill(result,1)
        with self.outage():
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda n:self.authority.exit_all(self.service_token,'concurrent-emergency-'+str(n)),range(8)))
            self.tick(result,6005);self.tick(result,6002)
            self.assertTrue(self.engine(result).portfolio['emergency'])
            self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
            self.assert_owned_safe(result,5)

    def test_late_duplicate_fill_during_outage_is_reconciled_without_overselling(self):
        result=self.enter();self.fill(result,1);entry=self.orders(result)[0]
        with self.outage():
            self.authority.exit_all(self.service_token,str(uuid.uuid4()));self.lifecycle.supervise(self.service_token)
            for _ in range(2):self.fixture.fill(entry['order_ref'],2,'10',self.wall(),'outage-late.01')
            self.lifecycle.supervise(self.service_token);self.assert_owned_safe(result,7)
            for _ in range(2):self.fill(result,side='SELL',price='9.9')
            self.assertFalse(self.engine(result).positions())
            self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])
        self.lifecycle.supervise(self.service_token)
        self.assertTrue(self.last(result)['reconciled_flat'])

    def test_lost_broker_connection_during_foundation_outage_does_not_forge_session(self):
        result=self.enter();self.fill(result,1)
        with self.outage():
            self.lifecycle.disconnect(self.service_token)
            self.assertFalse(self.fixture.connected)
            with self.assertRaises(Exception):self.tick(result,6005)
            self.assert_owned_safe(result,5)
            self.assertFalse(self.orders(result,'SELL'))

    def test_missing_protective_market_data_is_explicitly_critical_without_fake_execution(self):
        result=self.enter();self.fill(result,1);self.now+=2
        with self.outage():
            status=self.lifecycle.supervise(self.service_token)
            self.assertEqual(status['protective_condition'],'CRITICAL_UNRESOLVED_MARKET_DATA')
            self.assertTrue(status['entry_fenced'])
            self.assert_owned_safe(result,5)
            self.assertFalse(self.orders(result,'SELL'))
            alerts=[e for e in self.lifecycle.checkpoint.store.security_events() if e['kind']=='PROTECTIVE_HEALTH_CHANGED']
            self.assertTrue(alerts[-1]['details']['critical'])
            from executor_blind_presentation import dashboard_response
            public=dashboard_response('/api/status',{'lifecycle':status})
            self.assertTrue(all(t['risk_control_health']=='CRITICAL_UNRESOLVED' for t in public['lifecycle']['trades']))

    def test_missing_bound_journal_and_checkpoint_tampering_are_not_rebuilt(self):
        result=self.enter();self.fill(result,1);self.lifecycle.close();self.lifecycle=None
        (self.root/'service'/(result['trade_id']+'.sqlite')).rename(self.root/'preserved-journal.sqlite')
        from executor_lifecycle import OfflineExecutionLifecycle
        from executor_paper_broker_test import approval
        self.lifecycle=OfflineExecutionLifecycle(self.core,self.root/'service',approval(),self.fixture,self.service_token,self.wall,self.mono,start_worker=False)
        self.assertNotIn(result['trade_id'],self.lifecycle.engines)
        self.assertTrue(self.lifecycle.checkpoint.read()['fenced'])
        with self.lifecycle.checkpoint.store.database() as db:db.execute("UPDATE protective_checkpoint SET payload='{}'")
        self.assertEqual(self.lifecycle.protective_health(self.service_token)['condition'],'CRITICAL_UNRESOLVED_PROTECTION')

    def test_recovery_key_replacement_during_open_trade_does_not_revoke_protective_capability(self):
        result=self.enter();self.fill(result,1)
        from mortificatio_test_authenticator import OfflineAuthenticator
        options=self.security.begin_key_change(self.session.token,self.session.csrf_token,'127.0.0.1','ADD_RECOVERY')
        registration=self.security.finish_key_change(self.session.token,self.session.csrf_token,options['ceremony_token'],self.authenticator.assertion(options['public_key']),'127.0.0.1')
        key=OfflineAuthenticator()
        self.security.finish_key_registration(registration['ceremony_token'],key.registration(registration['public_key']),'127.0.0.1',self.session.token,self.session.csrf_token)
        proof=self.security.begin_recovery('127.0.0.1')
        replacement=self.security.finish_recovery_proof(proof['ceremony_token'],key.assertion(proof['public_key']),'127.0.0.1')
        self.security.finish_key_registration(replacement['ceremony_token'],OfflineAuthenticator().registration(replacement['public_key']),'127.0.0.1',recovery=True)
        self.tick(result,6004);self.tick(result,6001.25)
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
        self.assert_owned_safe(result,5)

    def test_real_child_cold_start_without_foundation_and_crash_preserves_reductions(self):
        result=self.enter();self.fill(result,1);self.tick(result,6020)
        self.lifecycle.close();self.lifecycle=None
        foundation=self.security.store.path;foundation.rename(foundation.with_suffix('.preserved'))
        code=r'''
import json,sys,os,uuid
from pathlib import Path
from datetime import datetime,timezone,timedelta
from executor_protection_recovery import recover_offline_protection
from executor_offline_fixture import OfflineBrokerFixture
from executor_paper_broker_test import approval,ACCOUNT,NOW
from executor_allocation_test import allocation_policy
from mortificatio_models import AccountBinding,ApplicationPolicy
from mortificatio_protection import ProtectionAuthority
from executor_paper_types import PaperSessionEvidence
from simulation_evidence import SPXObservation,FeedStatus
c=json.load(sys.stdin);root=Path(c['root']);wall=lambda:datetime.fromtimestamp(c['now'],timezone.utc);mono=lambda:100.
binding=AccountBinding(ACCOUNT,'PAPER','risk-v1');authority=ProtectionAuthority(root/'protection.sqlite',binding)
book=OfflineBrokerFixture(root/'fixture.sqlite',ACCOUNT,wall,mono,NOW-timedelta(days=1))
worker=recover_offline_protection(root/'service',approval(),book,authority,c['token'],ApplicationPolicy(binding),allocation_policy(),wall,mono,start_worker=False)
worker.verify_broker_session(c['token'],PaperSessionEvidence(ACCOUNT,(ACCOUNT,),'synthetic-paper-approval',str(uuid.uuid4()),wall(),wall()+timedelta(minutes=30),3))
worker.observe_spx(c['token'],c['trade'],'child-reversal',SPXObservation(6017.,FeedStatus.LIVE,wall(),mono()))
engine=worker.engines[c['trade']]
print(json.dumps(dict(peak=engine.portfolio['peak'],owned=sum(engine.positions().values()),sells=sum(i['quantity'] for i in engine.state['intents'].values() if i['side']=='SELL' and i['order_id'] is not None))),flush=True)
os._exit(0)
'''
        data=dict(root=str(self.root),now=self.now,token=self.service_token,trade=result['trade_id'])
        child=subprocess.run([sys.executable,'-B','-c',code],input=json.dumps(data),text=True,capture_output=True,timeout=20)
        self.assertEqual(child.returncode,0,child.stderr)
        report=json.loads(child.stdout);self.assertEqual(report,dict(peak='6020',owned=5,sells=5))
        self.assertFalse(foundation.exists())
        from executor_protection_recovery import recover_offline_protection
        from executor_paper_broker_test import approval
        self.lifecycle=recover_offline_protection(self.root/'service',approval(),self.fixture,self.authority,self.service_token,
            self.security.application_policy,self.allocation.policy,self.wall,self.mono,start_worker=False)
        self.verify();self.assert_owned_safe(result,5)
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
        with self.assertRaises(OSError):self.lifecycle.status(self.session.token)
        self.assertFalse(foundation.exists())


if __name__=='__main__':unittest.main()
