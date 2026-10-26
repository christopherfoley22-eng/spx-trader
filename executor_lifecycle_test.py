"""Offline real-auth lifecycle integration; independent durable synthetic broker."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from threading import Event
from decimal import Decimal
import json
import time
import unittest
import uuid
from unittest.mock import patch

import executor_application_test as app_fixtures
import executor_allocation_test as alloc_fixtures
from executor_application import UnifiedApplication
from executor_application_api import FoundationAPI,APIRequest,SESSION_COOKIE
from executor_allocation_service import AllocationBoundary
from executor_lifecycle import OfflineExecutionLifecycle
from executor_offline_fixture import OfflineBrokerFixture
from executor_paper_broker_test import approval,NOW,ACCOUNT
from executor_paper_types import PaperSessionEvidence,CommissionRecord,PaperBrokerError
from mortificatio_models import ApplicationError,TradeCommand
from simulation_evidence import SPXObservation,FeedStatus


class LiveFixtureProvider:
    def __init__(self,owner):self.owner=owner;self.funds='200000'
    def snapshot(self,direction):
        owner=self.owner;wall=owner.wall();mono=owner.mono();data=alloc_fixtures.evidence(direction,self.funds)
        quotes=tuple(replace(q,quote_id=q.quote_id+':'+str(owner.broker.generation)+':'+str(mono)+':'+str(owner.spx),
            option=replace(q.option,source_time=wall,received_monotonic=mono),
            spx=SPXObservation(owner.spx,FeedStatus.LIVE,wall,mono),sizes_source_time=wall,sizes_received_monotonic=mono,
            greeks_source_time=wall,greeks_received_monotonic=mono) for q in data.quotes)
        return alloc_fixtures.sync_protection_plan(replace(data,generation=owner.broker.generation,quotes=quotes,now_monotonic=mono,
            capacity_context=replace(data.capacity_context,generation=owner.broker.generation,observed_at=wall,received_monotonic=mono),
            account=replace(data.account,oldest_required_receipt_monotonic=mono),
            exposure=replace(data.exposure,generation=owner.broker.generation,observed_at=wall,received_monotonic=mono)))


class LifecycleTest(unittest.TestCase):
    enroll=app_fixtures.ApplicationTest.enroll
    login=app_fixtures.ApplicationTest.login
    grant=app_fixtures.ApplicationTest.grant
    authorize=app_fixtures.ApplicationTest.authorize

    def setUp(self):
        app_fixtures.ApplicationTest.setUp(self)
        self.initial=self.now;self.spx=6000.
        self.provider=LiveFixtureProvider(self)
        self.allocation=AllocationBoundary(alloc_fixtures.allocation_policy(),self.provider)
        self.core=UnifiedApplication(self.security,self.authority,self.broker,self.allocation)
        self.fixture=OfflineBrokerFixture(self.root/'fixture.sqlite',ACCOUNT,self.wall,self.mono,NOW-timedelta(days=1))
        self.lifecycle=OfflineExecutionLifecycle(self.core,self.root/'service',approval(),self.fixture,self.service_token,self.wall,self.mono,start_worker=False)
        self.addCleanup(self.cleanup_lifecycle)
        self.app=self.lifecycle
        self.verify()

    def cleanup_lifecycle(self):
        if self.lifecycle is not None:self.lifecycle.close();self.lifecycle=None

    def wall(self):return datetime.fromtimestamp(self.now,timezone.utc)
    def mono(self):return 100.+self.now-self.initial

    def verify(self,**changes):
        evidence=PaperSessionEvidence(ACCOUNT,(ACCOUNT,),'synthetic-paper-approval',str(uuid.uuid4()),self.wall(),
            self.wall()+timedelta(minutes=30),3)
        return self.lifecycle.verify_broker_session(self.service_token,replace(evidence,**changes))

    def enter(self,direction='CALL',identity=None):
        self.authorize()
        epoch=self.core.status(self.session.token)['entry_epoch']
        return self.lifecycle.direction_command(self.session.token,self.session.csrf_token,TradeCommand(identity or str(uuid.uuid4()),direction,epoch))

    def engine(self,result):return self.lifecycle.engines[result['trade_id']]

    def orders(self,result,side='BUY'):
        return [i for i in self.engine(result).state['intents'].values() if i['side']==side and i['order_id'] is not None]

    def fill(self,result,capacity=25,side='BUY',price='10',fee='0.65'):
        for item in self.orders(result,side):
            remaining=item['quantity']-self.engine(result).filled(item)
            quantity=min(capacity,remaining)
            if quantity:self.fixture.fill(item['order_ref'],quantity,price,self.wall(),str(uuid.uuid4())+'.01',fee)
        return self.lifecycle.supervise(self.service_token)

    def last(self,result):return self.lifecycle.status(self.session.token)['lifecycle']['trades'][list(self.lifecycle.engines).index(result['trade_id'])]

    def restart(self):
        self.lifecycle.close();self.lifecycle=None
        self.fixture=OfflineBrokerFixture(self.root/'fixture.sqlite',ACCOUNT,self.wall,self.mono,NOW-timedelta(days=1))
        self.lifecycle=OfflineExecutionLifecycle(self.core,self.root/'service',approval(),self.fixture,self.service_token,self.wall,self.mono,start_worker=False)
        self.app=self.lifecycle;self.broker.generation=self.lifecycle.anchor.state['generation']+1
        return self.verify()

    def test_call_one_command_allocates_reserves_and_binds_125_without_fabricating_fills(self):
        result=self.enter();engine=self.engine(result)
        self.assertEqual(result['allocation']['total_contracts'],125,result)
        self.assertEqual(sum(i['quantity'] for i in self.orders(result)),125,result)
        self.assertEqual(engine.positions(),{})
        self.assertEqual(result['lifecycle']['state'],'ACQUIRING')
        self.assertFalse(result['lifecycle']['reconciled_flat'])
        self.assertFalse(result['submission_enabled'])
        self.fill(result,2);self.assertEqual(sum(engine.positions().values()),10)
        self.fill(result);self.assertEqual(sum(engine.positions().values()),125)

    def test_put_entry_and_uncapped_spx_trailing(self):
        result=self.enter('PUT');self.fill(result)
        engine=self.engine(result)
        for serial,price in enumerate((5996.,5995.,5990.,5980.,5950.)):
            self.lifecycle.observe_spx(self.service_token,result['trade_id'],'spx:'+str(serial),SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))
            self.assertIsNone(engine.portfolio['exit_reason'])
        self.lifecycle.observe_spx(self.service_token,result['trade_id'],'reverse',SPXObservation(5953.,FeedStatus.LIVE,self.wall(),self.mono()))
        self.assertTrue(engine.portfolio['exit_reason'])
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),125)
        self.assertFalse(self.last(result)['reconciled_flat'])
        self.fill(result,side='SELL',price='9.9');self.assertTrue(self.last(result)['reconciled_flat'])

    def test_independent_premium_stop_and_partial_entry_cancellation(self):
        result=self.enter();self.fill(result,2)
        q=self.provider.snapshot('CALL').quotes[alloc_fixtures.STRIKES.index(6000.)]
        q=replace(q,quote_id='premium-stop',option=replace(q.option,bid=6.75,ask=6.85))
        self.lifecycle.observe_liquidity(self.service_token,result['trade_id'],'premium-stop',q)
        engine=self.engine(result)
        self.assertEqual(engine.portfolio['exit_reason'],'INITIAL_STOP')
        self.assertTrue(all(i['terminal_proven'] for i in self.orders(result)))
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),10)
        self.assertFalse(self.last(result)['reconciled_flat'])

    def test_emergency_during_acquisition_cancels_and_plans_owned_only(self):
        result=self.enter();self.fill(result,3)
        identity=str(uuid.uuid4())
        self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,identity)
        self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,identity)
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),15)
        self.assertTrue(self.authority.emergency.blocked())
        self.assertFalse(self.last(result)['reconciled_flat'])
        self.fill(result,side='SELL',price='9.9')
        self.assertTrue(self.last(result)['reconciled_flat'])
        with self.assertRaises(ApplicationError):self.enter('PUT')

    def test_grant_expiry_cancels_pending_buys_without_dashboard_or_new_authority(self):
        result=self.enter();self.fill(result,1);self.now+=181
        self.lifecycle.supervise(self.service_token)
        self.assertTrue(self.engine(result).portfolio['halted'])
        self.assertTrue(all(i['terminal_proven'] for i in self.orders(result)))
        self.assertEqual(sum(self.engine(result).positions().values()),5)
        self.assertFalse(self.last(result)['reconciled_flat'])
        with self.assertRaises(ApplicationError):self.core.direction_command(self.session.token,self.session.csrf_token,TradeCommand(str(uuid.uuid4()),'CALL',1))

    def test_dashboard_expiry_still_allows_independent_stop_and_reconciliation(self):
        result=self.enter();self.fill(result,1);self.now+=901
        self.lifecycle.supervise(self.service_token)
        with self.assertRaises(ApplicationError):self.lifecycle.status(self.session.token)
        q=self.provider.snapshot('CALL').quotes[alloc_fixtures.STRIKES.index(6000.)]
        q=replace(q,quote_id='expired-stop',option=replace(q.option,bid=6.75,ask=6.85))
        state=self.lifecycle.observe_liquidity(self.service_token,result['trade_id'],'expired-stop',q)
        self.assertTrue(self.engine(result).portfolio['exit_reason'])
        # Other stale strikes retain exposure until their own valid quotes arrive.
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),1)
        for fresh in self.provider.snapshot('CALL').quotes:
            if str(fresh.option.contract.con_id) in self.engine(result).portfolio['targets'] and fresh.option.contract.con_id!=q.option.contract.con_id:
                identity=str(uuid.uuid4())
                self.lifecycle.observe_liquidity(self.service_token,result['trade_id'],identity,replace(fresh,quote_id=identity))
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
        for item in self.orders(result,'SELL'):
            self.fixture.fill(item['order_ref'],item['quantity'],item['limit_price'],self.wall(),str(uuid.uuid4())+'.01')
        self.lifecycle.supervise(self.service_token)
        self.assertTrue(self.lifecycle.supervise(self.service_token)['trades'][0]['reconciled_flat'])

    def test_disconnect_reconnect_reconciles_without_reacquisition(self):
        result=self.enter();self.fill(result,2);before=len(self.engine(result).state['intents'])
        self.lifecycle.disconnect(self.service_token)
        status=self.lifecycle.supervise(self.service_token)
        self.assertFalse(status['account_reconciled']);self.assertFalse(status['trades'][0]['reconciled_flat'])
        with self.assertRaises((ApplicationError,PaperBrokerError)):self.authorize()
        self.broker.generation=2;self.verify()
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(len(self.engine(result).state['intents']),before)
        self.assertTrue(self.engine(result).portfolio['halted'])
        self.assertEqual(sum(self.engine(result).positions().values()),10)

    def test_restart_retains_positions_peak_pending_orders_and_never_retries_entry(self):
        result=self.enter();self.fill(result,2)
        self.lifecycle.observe_spx(self.service_token,result['trade_id'],'arm',SPXObservation(6005.,FeedStatus.LIVE,self.wall(),self.mono()))
        references=[i['order_ref'] for i in self.orders(result)]
        self.restart();self.lifecycle.supervise(self.service_token)
        self.assertEqual(self.engine(result).portfolio['peak'],'6005.0')
        self.assertTrue(self.engine(result).portfolio['ride'])
        self.assertEqual([i['order_ref'] for i in self.orders(result)],references)
        self.assertEqual(sum(self.engine(result).positions().values()),10)
        self.lifecycle.observe_spx(self.service_token,result['trade_id'],'reverse',SPXObservation(6002.,FeedStatus.LIVE,self.wall(),self.mono()))
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),10)

    def test_late_and_duplicate_fills_require_additional_reduction_not_shorting(self):
        result=self.enter();self.fill(result,1)
        self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        entry=self.orders(result)[0];identity='late.01'
        self.fixture.fill(entry['order_ref'],2,'10',self.wall(),identity)
        self.fixture.fill(entry['order_ref'],2,'10',self.wall(),identity)
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(sum(self.engine(result).positions().values()),7)
        self.assertLessEqual(sum(i['quantity']-self.engine(result).filled(i) for i in self.orders(result,'SELL')),7)
        self.fill(result,side='SELL',price='9.9')
        self.lifecycle.supervise(self.service_token)
        self.fill(result,side='SELL',price='9.9')
        self.assertTrue(self.last(result)['reconciled_flat'])
        self.assertFalse(self.engine(result).positions())

    def test_concurrent_commands_reserve_and_dispatch_exactly_once(self):
        self.authorize()
        def enter(_):
            try:return self.lifecycle.direction_command(self.session.token,self.session.csrf_token,TradeCommand(str(uuid.uuid4()),'CALL',0))
            except ApplicationError:return None
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(enter,range(6)))
        accepted=[r for r in results if r is not None];self.assertEqual(len(accepted),1)
        self.assertEqual(len(self.orders(accepted[0])),5)

    def test_duplicate_command_never_redrives_after_revocation(self):
        result=self.enter();identity=result['allocation']['command_id'];before=len(self.orders(result))
        self.now+=181
        duplicate=self.lifecycle.direction_command(self.session.token,self.session.csrf_token,TradeCommand(identity,'CALL',0))
        self.assertEqual(duplicate['result'],'DUPLICATE');self.assertEqual(len(self.orders(result)),before)

    def test_authorization_revocation_does_not_revoke_protective_service(self):
        result=self.enter();self.fill(result,1)
        self.security.logout(self.session.token,self.session.csrf_token)
        self.lifecycle.supervise(self.service_token)
        self.assertTrue(self.engine(result).portfolio['halted'])
        self.authority.require(self.service_token,'RECONCILE')
        self.assertTrue(all(i['terminal_proven'] for i in self.orders(result)))

    def test_wrong_account_live_environment_read_only_or_unverified_sessions_rejected(self):
        self.lifecycle.disconnect(self.service_token)
        for changes in ({'account':'U1234567'},{'managed_accounts':('DU9999999',)},{'read_only':False},{'mode':'LIVE'},{'origin':'REAL_IBKR'}):
            with self.subTest(changes=changes),self.assertRaises(PaperBrokerError):self.verify(**changes)
        with self.assertRaises((ApplicationError,PaperBrokerError)):self.enter()

    def test_missing_commissions_prevents_false_flat_and_cash_release(self):
        result=self.enter();self.fill(result,1)
        self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        for item in self.orders(result,'SELL'):
            self.fixture.fill(item['order_ref'],item['quantity'],'9.9',self.wall(),'no-fee:'+str(item['order_id'])+'.01',fee=None)
        self.lifecycle.supervise(self.service_token)
        self.assertFalse(self.engine(result).positions());self.assertFalse(self.last(result)['reconciled_flat'])
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT state FROM allocation_reservations').fetchone()[0],'HELD')
        for item in self.orders(result,'SELL'):
            identity='no-fee:'+str(item['order_id'])+'.01'
            self.fixture.commission(result['trade_id'],CommissionRecord('late-fee:'+identity,identity,'.65'))
        self.lifecycle.supervise(self.service_token);self.assertTrue(self.last(result)['reconciled_flat'])

    def test_binding_failure_is_uncertain_and_never_resubmitted(self):
        self.lifecycle.fault_hook=lambda stage,trade:(_ for _ in ()).throw(OSError('fixture interruption')) if stage=='AFTER_MODEL_BINDING' else None
        result=self.enter();self.assertEqual(result['lifecycle']['state'],'PENDING_RECONCILIATION')
        refs=[i['order_ref'] for i in self.orders(result)]
        for _ in range(3):self.lifecycle.supervise(self.service_token)
        self.assertEqual([i['order_ref'] for i in self.orders(result)],refs)
        self.assertFalse(self.last(result)['reconciled_flat'])
        with self.fixture.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM fixture_orders').fetchone()[0],0)

    def test_status_api_hides_unrealized_pnl_and_reports_synthetic_identity(self):
        result=self.enter();self.fill(result,1)
        response=FoundationAPI(self.lifecycle).dispatch(APIRequest('GET','/api/status',
            {'Host':'mortificatio.test','Cookie':SESSION_COOKIE+'='+self.session.token},secure_transport=True,peer_id='127.0.0.1'))
        self.assertEqual(response.status,200)
        raw=json.dumps(response.body).lower()
        for forbidden in ('unrealized','pnl','p&l','live_return'):self.assertNotIn(forbidden,raw)
        self.assertFalse(response.body['lifecycle']['real_ibkr_authenticated'])
        self.assertFalse(response.body['submission_enabled'])

    def test_service_authority_cannot_create_entry_or_login(self):
        with self.assertRaises(ApplicationError):self.lifecycle.direction_command(self.service_token,self.service_token,TradeCommand(str(uuid.uuid4()),'CALL',0))
        self.authority.require(self.service_token,'OBSERVE_RISK')
        with self.assertRaises(ApplicationError):self.authority.require(self.service_token,'ENTRY')

    def test_no_network_or_real_order_boundaries(self):
        with patch('socket.socket',side_effect=AssertionError('no network')):
            result=self.enter();self.fill(result,1);self.lifecycle.supervise(self.service_token)
            self.assertFalse(self.last(result)['submission_enabled'])

    def test_valid_spx_before_first_fill_does_not_halt_or_arm_a_position(self):
        result=self.enter()
        self.lifecycle.observe_spx(self.service_token,result['trade_id'],'before-fill',SPXObservation(6050.,FeedStatus.LIVE,self.wall(),self.mono()))
        self.assertFalse(self.engine(result).portfolio['halted'])
        self.assertFalse(self.engine(result).portfolio['ride'])
        self.assertEqual(Decimal(self.engine(result).portfolio['peak']),6000)
        self.fill(result,1)
        self.lifecycle.observe_spx(self.service_token,result['trade_id'],'after-fill',SPXObservation(6004.,FeedStatus.LIVE,self.wall(),self.mono()))
        self.assertTrue(self.last(result)['early_protection_active'])
        self.assertIsNone(self.engine(result).portfolio['exit_reason'])


if __name__=='__main__':unittest.main()
