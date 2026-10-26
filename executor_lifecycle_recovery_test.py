"""Offline crash boundaries, account-wide IDs, independent worker and recovery."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

import executor_lifecycle_test as fixtures
from executor_lifecycle import OfflineExecutionLifecycle
from executor_application import UnifiedApplication
from executor_paper_broker_test import approval,NOW,ACCOUNT
from executor_paper_types import PaperBrokerError
from mortificatio_auth import MortificatioSecurity
from mortificatio_models import ApplicationError,TradeCommand
from simulation_evidence import SPXObservation,FeedStatus


class RecoveryTest(unittest.TestCase):
    setUp=fixtures.LifecycleTest.setUp
    cleanup_lifecycle=fixtures.LifecycleTest.cleanup_lifecycle
    enroll=fixtures.LifecycleTest.enroll
    login=fixtures.LifecycleTest.login
    grant=fixtures.LifecycleTest.grant
    authorize=fixtures.LifecycleTest.authorize
    wall=fixtures.LifecycleTest.wall
    mono=fixtures.LifecycleTest.mono
    verify=fixtures.LifecycleTest.verify
    enter=fixtures.LifecycleTest.enter
    engine=fixtures.LifecycleTest.engine
    orders=fixtures.LifecycleTest.orders
    fill=fixtures.LifecycleTest.fill
    last=fixtures.LifecycleTest.last
    restart=fixtures.LifecycleTest.restart

    def test_publication_outbox_replays_after_commit_before_acknowledgment_crash(self):
        result=self.enter()
        def fault(stage,trade):
            if stage=='AFTER_LIFECYCLE_PUBLICATION':raise OSError('publication acknowledgement interrupted')
        self.lifecycle.fault_hook=fault
        status=self.fill(result,1)
        self.assertEqual(status['protective_condition'],'CRITICAL_UNRESOLVED_PROTECTION')
        self.assertTrue(self.lifecycle.checkpoint.read()['fenced'])
        with self.security.store.database() as db:
            self.assertGreater(db.execute('SELECT count(*) FROM execution_publications WHERE delivered=0').fetchone()[0],0)
        self.restart();self.lifecycle.supervise(self.service_token)
        self.assertEqual(sum(self.engine(result).positions().values()),5)
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM execution_publications WHERE delivered=0').fetchone()[0],0)
            self.assertTrue(db.execute('SELECT ever_filled FROM commands').fetchone()[0])
        self.assertFalse(self.last(result)['reconciled_flat'])

    def test_partial_fixture_acceptance_leaves_other_bindings_uncertain_without_retry(self):
        def fault(stage,trade):
            if stage=='AFTER_FIXTURE_BINDING':raise OSError('one strike accepted before interruption')
        self.lifecycle.fault_hook=fault
        result=self.enter();self.lifecycle.fault_hook=lambda *args:None
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(len(self.orders(result)),5)
        with self.fixture.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM fixture_orders').fetchone()[0],1)
        self.assertFalse(self.last(result)['reconciled_flat'])
        self.assertFalse(self.lifecycle._fresh_account())
        self.restart();self.lifecycle.supervise(self.service_token)
        with self.fixture.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM fixture_orders').fetchone()[0],1)

    def test_unconfigured_reserved_command_never_releases_cash_or_restarts_acquisition(self):
        self.lifecycle.fault_hook=lambda stage,trade:(_ for _ in ()).throw(OSError('reserved only')) if stage=='AFTER_RESERVATION' else None
        result=self.enter();self.restart();self.lifecycle.supervise(self.service_token)
        # A missing catalogued journal is not silently recreated on restart.
        self.assertNotIn(result['trade_id'],self.lifecycle.engines)
        self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT state FROM allocation_reservations').fetchone()[0],'HELD')
        with self.assertRaises(ApplicationError):self.enter('PUT')

    def test_configured_never_bound_intents_can_reconcile_zero_fill_without_reacquiring(self):
        self.lifecycle.fault_hook=lambda stage,trade:(_ for _ in ()).throw(OSError('configured only')) if stage=='AFTER_CONFIGURATION' else None
        result=self.enter();self.restart();self.lifecycle.supervise(self.service_token)
        self.assertFalse(self.engine(result).state['intents'])
        self.assertTrue(self.last(result)['reconciled_flat'])
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT state FROM allocation_reservations').fetchone()[0],'RELEASED')

    def test_global_ids_refs_and_account_frequency_across_two_complete_trades(self):
        first=self.enter();self.fill(first,1)
        for event,price in (('early-arm',6004.),('early-exit',6001.25)):
            self.lifecycle.observe_spx(self.service_token,first['trade_id'],event,SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))
        self.fill(first,side='SELL',price='9.9');self.assertTrue(self.last(first)['reconciled_flat'])
        second=self.enter('PUT');self.assertEqual(len(self.orders(second)),5)
        all_intents=[i for engine in self.lifecycle.engines.values() for i in engine.state['intents'].values() if i['order_id'] is not None]
        self.assertEqual(len({i['order_ref'] for i in all_intents}),len(all_intents))
        self.assertEqual(len({i['order_id'] for i in all_intents}),len(all_intents))
        self.fill(second,1)
        for event,price in (('put-arm',5996.),('put-exit',5998.75)):
            self.lifecycle.observe_spx(self.service_token,second['trade_id'],event,SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))
        self.fill(second,side='SELL',price='9.9');self.assertTrue(self.last(second)['reconciled_flat'])
        with self.assertRaises(ApplicationError) as error:self.enter('CALL')
        self.assertEqual(error.exception.code,'ACCOUNT_TRADE_FREQUENCY_LIMIT')

    def test_fresh_trade_after_reconnect_binds_global_epoch_to_new_local_journal(self):
        result=self.enter();self.fill(result,1)
        for event,price in (('arm',6004.),('exit',6001.25)):
            self.lifecycle.observe_spx(self.service_token,result['trade_id'],event,SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))
        self.fill(result,side='SELL',price='9.9');self.assertTrue(self.last(result)['reconciled_flat'])
        self.lifecycle.disconnect(self.service_token);self.broker.generation=2;self.verify()
        next_trade=self.enter('PUT');engine=self.engine(next_trade)
        self.assertEqual(engine.state['generation'],1)
        self.assertEqual(engine.portfolio['broker_context']['broker_generation'],2)
        self.assertEqual(len(self.orders(next_trade)),5)

    def test_queued_late_fill_is_reconciled_before_any_new_trade_admission(self):
        result=self.enter();self.fill(result,1)
        for event,price in (('arm',6004.),('exit',6001.25)):
            self.lifecycle.observe_spx(self.service_token,result['trade_id'],event,SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))
        self.fill(result,side='SELL',price='9.9');self.assertTrue(self.last(result)['reconciled_flat'])
        item=self.orders(result)[0]
        self.fixture.fill(item['order_ref'],1,'10',self.wall(),'after-flat.01')
        with self.assertRaises(ApplicationError):self.enter('PUT')
        self.assertEqual(len(self.lifecycle.engines),1)
        self.assertEqual(sum(self.engine(result).positions().values()),1)
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT state FROM allocation_reservations').fetchone()[0],'HELD')
            self.assertTrue(db.execute('SELECT blocked FROM account_state').fetchone()[0])
        self.lifecycle.supervise(self.service_token)
        self.assertFalse(self.last(result)['reconciled_flat'])

    def test_duplicate_concurrent_uuid_never_redrives_the_model(self):
        self.authorize();identity=str(uuid.uuid4());command=TradeCommand(identity,'CALL',0)
        with ThreadPoolExecutor(max_workers=5) as workers:
            results=list(workers.map(lambda _:self.lifecycle.direction_command(self.session.token,self.session.csrf_token,command),range(5)))
        self.assertEqual(sum(r['result']=='ACCEPTED_OFFLINE' for r in results),1)
        with self.fixture.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM fixture_orders').fetchone()[0],5)

    def test_background_supervisor_cancels_after_revocation_without_dashboard_calls(self):
        result=self.enter();self.fill(result,1)
        self.security.logout(self.session.token,self.session.csrf_token)
        self.lifecycle.start_supervision(self.service_token,interval=.02)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with self.engine(result).lock:
                if all(i['terminal_proven'] for i in self.orders(result)):break
            time.sleep(.02)
        self.assertTrue(all(i['terminal_proven'] for i in self.orders(result)))
        self.assertTrue(self.lifecycle.thread.is_alive())
        self.assertEqual(sum(self.engine(result).positions().values()),5)

    def test_default_lifecycle_starts_its_independent_supervisor(self):
        self.lifecycle.close();self.lifecycle=None
        self.lifecycle=OfflineExecutionLifecycle(self.core,self.root/'service',approval(),self.fixture,self.service_token,self.wall,self.mono)
        self.app=self.lifecycle;self.broker.generation=self.lifecycle.anchor.state['generation']+1;self.verify()
        self.assertTrue(self.lifecycle.thread.is_alive())
        result=self.enter();self.security.logout(self.session.token,self.session.csrf_token)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with self.lifecycle.lock:
                if all(i['terminal_proven'] for i in self.orders(result)):break
            time.sleep(.02)
        self.assertTrue(all(i['terminal_proven'] for i in self.orders(result)))

    def test_background_checks_do_not_extend_idle_dashboard_session(self):
        self.lifecycle.close();self.lifecycle=None
        self.security=MortificatioSecurity(self.root/'idle.sqlite',replace(self.security.policy,idle_ttl=2),self.security.application_policy,lambda:self.now)
        self.enroll();self.session=self.login()
        self.core=UnifiedApplication(self.security,self.authority,self.broker,self.allocation)
        self.lifecycle=OfflineExecutionLifecycle(self.core,self.root/'idle-service',approval(),self.fixture,self.service_token,self.wall,self.mono,start_worker=False)
        self.app=self.lifecycle;self.verify();result=self.enter()
        self.now+=.6;self.lifecycle.supervise(self.service_token)
        self.now+=1.5;self.lifecycle.supervise(self.service_token)
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT revoked FROM sessions').fetchone()[0],1)
        self.assertTrue(self.engine(result).portfolio['halted'])
        self.assertTrue(self.last_without_login(result)['reconciled_flat'])

    def last_without_login(self,result):return self.lifecycle._trade_status(result['trade_id'])

    def test_stale_reconciliation_never_releases_zero_owned_position(self):
        result=self.enter();self.fill(result,1)
        self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        for item in self.orders(result,'SELL'):
            self.fixture.fill(item['order_ref'],item['quantity'],'9.9',self.wall(),str(uuid.uuid4())+'.01')
        original=self.fixture.snapshot
        def stale(generation):
            snapshot=original(generation)
            return replace(snapshot,component_receipts=tuple((name,self.mono()-1.01) for name,_ in snapshot.component_receipts))
        with patch.object(self.fixture,'snapshot',side_effect=stale):
            self.lifecycle.supervise(self.service_token)
            self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])
            with self.security.store.database() as db:self.assertEqual(db.execute('SELECT state FROM allocation_reservations').fetchone()[0],'HELD')
        self.lifecycle.supervise(self.service_token);self.assertTrue(self.last(result)['reconciled_flat'])

    def test_invalid_market_event_halts_acquisition_without_weakening_quote_gate(self):
        result=self.enter();self.fill(result,1)
        quote=self.provider.snapshot('CALL').quotes[4]
        quote=replace(quote,quote_id='missing-source',option=replace(quote.option,source_time=None))
        with self.assertRaises(PaperBrokerError):self.lifecycle.observe_liquidity(self.service_token,result['trade_id'],'invalid-source',quote)
        self.lifecycle.supervise(self.service_token)
        self.assertTrue(self.engine(result).portfolio['halted'])
        self.assertTrue(all(i['terminal_proven'] for i in self.orders(result)))
        self.assertFalse(self.last(result)['reconciled_flat'])

    def test_writer_lock_prevents_two_service_owners(self):
        with self.assertRaises(ApplicationError):
            OfflineExecutionLifecycle(self.core,self.root/'service',approval(),self.fixture,self.service_token,self.wall,self.mono,start_worker=False)
        self.assertTrue(self.lifecycle._fresh_account())

    def test_service_capability_cannot_bind_buy_intents_as_reductions(self):
        result=self.enter();engine=self.engine(result)
        from mortificatio_protection import IndependentProtectionService
        engine.trigger_exit('reduce-only','STRATEGY_EXIT')
        with self.assertRaises(ApplicationError):
            IndependentProtectionService(self.authority,engine).simulate_reduction_bindings(self.service_token,[self.orders(result)[0]['intent_id']],2000)

    def test_actual_child_process_death_keeps_broker_book_and_no_entry_replay(self):
        result=self.enter();self.fill(result,1)
        self.lifecycle.close();self.lifecycle=None
        script='''
import json,os,sys,uuid
from pathlib import Path
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from mortificatio_auth import MortificatioSecurity,SecurityPolicy
from mortificatio_models import ApplicationPolicy
from mortificatio_protection import ProtectionAuthority
from executor_application import UnifiedApplication
from executor_application_test import FixtureBrokerBoundary
from executor_allocation_test import BINDING,allocation_policy
from executor_allocation_service import AllocationBoundary
from executor_lifecycle_test import LiveFixtureProvider
from executor_lifecycle import OfflineExecutionLifecycle
from executor_offline_fixture import OfflineBrokerFixture
from executor_paper_broker_test import approval,NOW,ACCOUNT
from executor_paper_types import PaperSessionEvidence
root=Path(sys.argv[1]);now=float(sys.argv[2]);credentials=json.load(sys.stdin)
wall=lambda:datetime.fromtimestamp(now,timezone.utc);mono=lambda:100.
security=MortificatioSecurity(root/'foundation.sqlite',SecurityPolicy('mortificatio.test','https://mortificatio.test'),ApplicationPolicy(BINDING),lambda:now)
authority=ProtectionAuthority(root/'protection.sqlite',BINDING);broker=FixtureBrokerBoundary(lambda:now,BINDING)
owner=SimpleNamespace(wall=wall,mono=mono,spx=6000.,broker=broker)
core=UnifiedApplication(security,authority,broker,AllocationBoundary(allocation_policy(),LiveFixtureProvider(owner)))
source=OfflineBrokerFixture(root/'fixture.sqlite',ACCOUNT,wall,mono,NOW-timedelta(days=1))
service=OfflineExecutionLifecycle(core,root/'service',approval(),source,credentials['service'],wall,mono,start_worker=False)
broker.generation=service.anchor.state['generation']+1
service.verify_broker_session(credentials['service'],PaperSessionEvidence(ACCOUNT,(ACCOUNT,),'synthetic-paper-approval',str(uuid.uuid4()),wall(),wall()+timedelta(minutes=30),3))
service.supervise(credentials['service'])
assert sum(next(iter(service.engines.values())).positions().values())==5
os._exit(74)
'''
        child=subprocess.run([sys.executable,'-B','-c',script,str(self.root),str(self.now)],
            input=json.dumps({'service':self.service_token}),capture_output=True,text=True,timeout=20)
        self.assertEqual(child.returncode,74,child.stderr)
        self.lifecycle=OfflineExecutionLifecycle(self.core,self.root/'service',approval(),self.fixture,self.service_token,self.wall,self.mono,start_worker=False)
        self.app=self.lifecycle;self.broker.generation=self.lifecycle.anchor.state['generation']+1
        self.verify();self.lifecycle.supervise(self.service_token)
        self.assertEqual(sum(self.engine(result).positions().values()),5)
        self.assertEqual(len(self.orders(result)),5)
        self.assertFalse(self.last(result)['reconciled_flat'])

    def test_closed_facade_cannot_admit_another_command_or_restart_worker(self):
        self.authorize();closed=self.lifecycle;self.cleanup_lifecycle()
        with self.assertRaises(ApplicationError) as error:
            closed.direction_command(self.session.token,self.session.csrf_token,TradeCommand(str(uuid.uuid4()),'CALL',0))
        self.assertEqual(error.exception.code,'LIFECYCLE_CLOSED')
        with self.assertRaises(ApplicationError):closed.start_supervision(self.service_token)
        closed.close()

    def test_put_weighted_premium_stop_uses_actual_partial_fill_prices(self):
        result=self.enter('PUT');item=self.orders(result)[0]
        self.fixture.fill(item['order_ref'],1,'9',self.wall(),'weighted-1.01')
        self.fixture.fill(item['order_ref'],1,'10',self.wall(),'weighted-2.01')
        self.lifecycle.supervise(self.service_token)
        stop=self.last(result)['premium_protection'][0]
        self.assertEqual(Decimal(stop['weighted_average_premium']),Decimal('9.5'))
        self.assertEqual(Decimal(stop['initial_premium_stop']),Decimal('6.25'))
        quote=self.provider.snapshot('PUT').quotes[4]
        quote=replace(quote,quote_id='weighted-stop',option=replace(quote.option,bid=6.25,ask=6.35))
        self.lifecycle.observe_liquidity(self.service_token,result['trade_id'],'weighted-stop',quote)
        self.assertEqual(self.engine(result).portfolio['exit_reason'],'INITIAL_STOP')
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),2)

    def test_source_execution_time_is_not_local_callback_receipt(self):
        result=self.enter();item=self.orders(result)[0];source=self.wall();self.now+=.5
        self.fixture.fill(item['order_ref'],1,'10',source,'source-time.01')
        self.lifecycle.supervise(self.service_token)
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT first_fill_at FROM commands').fetchone()[0],source.isoformat())
        self.assertNotEqual(self.engine(result).portfolio['execution_receipts']['source-time.01'],source.isoformat())

    def test_premium_stop_remains_effective_after_trailing_activation(self):
        result=self.enter();self.fill(result,1)
        self.lifecycle.observe_spx(self.service_token,result['trade_id'],'arm',SPXObservation(6005.,FeedStatus.LIVE,self.wall(),self.mono()))
        status=self.last(result)
        self.assertTrue(status['trailing_active']);self.assertFalse(status['early_protection_active'])
        self.assertEqual(Decimal(status['trailing_threshold']),Decimal('6002'))
        quote=self.provider.snapshot('CALL').quotes[4]
        quote=replace(quote,quote_id='hard-after-trail',option=replace(quote.option,bid=6.75,ask=6.85))
        self.lifecycle.observe_liquidity(self.service_token,result['trade_id'],'hard-after-trail',quote)
        self.assertEqual(self.engine(result).portfolio['exit_reason'],'INITIAL_STOP')


if __name__=='__main__':unittest.main()
