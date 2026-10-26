"""Phase 2 real authorization/SQLite + fake-only scheduler integration tests."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

import executor_application_test as app_fixtures
import executor_allocation_test as alloc_fixtures
from executor_application import UnifiedApplication
from executor_application_api import FoundationAPI,APIRequest,SESSION_COOKIE
from executor_allocation_service import AllocationBoundary
from executor_portfolio import OfflinePortfolioLedger
from executor_portfolio_test import PortfolioFakeBroker,policy
from executor_paper_broker_test import approval
from mortificatio_models import ApplicationError,TradeCommand
from mortificatio_auth import MortificatioSecurity
from executor_paper_types import PaperBrokerError
from mortificatio_protection import IndependentProtectionService


class FixtureAllocationProvider:
    def __init__(self):self.calls=0;self.change=lambda data:data
    def snapshot(self,direction):
        self.calls+=1
        return self.change(alloc_fixtures.evidence(direction))


class AllocationFakeBroker(PortfolioFakeBroker):
    def match(self,capacity=1000,side=None):
        available={int(cid):capacity for cid in self.ledger.portfolio['targets']}
        for key,record in list(self.orders.items()):
            if record.status not in {'Submitted','PreSubmitted','PendingCancel'} or side and record.side!=side:continue
            item=self.ledger.state['intents'][key];cid=record.contract.con_id
            remaining=record.quantity-sum(e.quantity for e in self.active.values() if e.order_ref==record.order_ref)
            price=Decimal('10') if record.side=='BUY' else Decimal('9.9')
            if record.side=='BUY' and Decimal(item['limit_price'])<price or record.side=='SELL' and Decimal(item['limit_price'])>price:continue
            quantity=min(remaining,available[cid])
            if quantity:self.execution(key,quantity,str(price));available[cid]-=quantity


class AllocationServiceTest(unittest.TestCase):
    enroll=app_fixtures.ApplicationTest.enroll
    login=app_fixtures.ApplicationTest.login
    grant=app_fixtures.ApplicationTest.grant
    authorize=app_fixtures.ApplicationTest.authorize
    command=app_fixtures.ApplicationTest.command
    evidence=app_fixtures.ApplicationTest.evidence
    finish=app_fixtures.ApplicationTest.finish

    def setUp(self):
        app_fixtures.ApplicationTest.setUp(self)
        self.provider=FixtureAllocationProvider()
        self.boundary=AllocationBoundary(alloc_fixtures.allocation_policy(),self.provider)
        self.app=UnifiedApplication(self.security,self.authority,self.broker,self.boundary)
        self.engines=[];self.addCleanup(lambda:[engine.close() for engine in self.engines])

    def engine(self,name='portfolio.sqlite'):
        engine=OfflinePortfolioLedger(self.root/name,approval(),policy());self.engines.append(engine)
        fake=AllocationFakeBroker(engine);fake.start();self.assertTrue(fake.reconcile()['matched'])
        return engine,fake

    def admitted(self):self.authorize();return self.command()

    def bound(self):
        result=self.admitted();engine,fake=self.engine()
        self.boundary.bind_engine(self.app,self.session.token,self.session.csrf_token,result['trade_id'],engine,alloc_fixtures.evidence().account)
        return result,engine,fake

    def schedule(self,result,engine):
        return self.boundary.advance(self.app,self.session.token,self.session.csrf_token,result['trade_id'],engine,alloc_fixtures.evidence().account)

    def dispatch(self,result,engine,fake,actions):
        ids=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']
        self.assertTrue(fake.reconcile()['matched'])
        self.boundary.simulate_dispatch(self.app,self.session.token,self.session.csrf_token,result['trade_id'],engine,ids,alloc_fixtures.evidence().account)
        for identity in ids:fake.order(identity)
        self.assertTrue(fake.reconcile()['matched'])
        return ids

    def held(self):
        with self.security.store.database() as db:return [dict(r) for r in db.execute('SELECT * FROM allocation_reservations')]

    def test_one_press_call_and_put_api_automatically_return_funded_plans(self):
        self.authorize();api=FoundationAPI(self.app)
        for direction in ('CALL','PUT'):
            body={'command_id':str(uuid.uuid4()),'direction':direction,'entry_epoch':self.app.status(self.session.token)['entry_epoch']}
            response=api.dispatch(APIRequest('POST','/api/commands',{'Host':'mortificatio.test','Origin':'https://mortificatio.test',
                'Content-Type':'application/json','Cookie':SESSION_COOKIE+'='+self.session.token,'X-CSRF-Token':self.session.csrf_token},json.dumps(body).encode(),True,'127.0.0.1'))
            self.assertEqual(response.status,200,response.body)
            self.assertEqual(response.body['allocation']['direction'],direction)
            self.assertFalse(response.body['submission_enabled'])
            engine,fake=self.engine(direction+'.sqlite')
            self.boundary.bind_engine(self.app,self.session.token,self.session.csrf_token,response.body['trade_id'],engine,alloc_fixtures.evidence().account)
            self.finish(response.body['trade_id'],filled=False)
            self.assertEqual(self.held()[-1]['state'],'HELD')
            self.boundary.release_reconciled(self.app,self.service_token,response.body['trade_id'],engine)
        self.assertTrue(all(row['state']=='RELEASED' for row in self.held()))

    def test_authentication_grant_and_preflight_fail_before_provider_is_called(self):
        with self.assertRaises(ApplicationError):self.command()
        self.assertEqual(self.provider.calls,0)
        self.authorize();self.broker.changes={'buying_power_passed':False}
        with self.assertRaises(ApplicationError):self.command()
        self.assertEqual(self.provider.calls,0)

    def test_duplicate_command_returns_immutable_existing_plan_without_reallocation(self):
        self.authorize();identity=str(uuid.uuid4());first=self.command(identity=identity,epoch=0)
        self.provider.change=lambda _:None
        second=self.command(identity=identity,epoch=0)
        self.assertEqual(first['allocation'],second['allocation']);self.assertEqual(self.provider.calls,1)
        self.assertEqual(len(self.held()),1)

    def test_invalid_evidence_writes_no_command_plan_or_reservation(self):
        self.authorize();self.provider.change=lambda e:replace(e,chain_complete=False)
        with self.assertRaises(ApplicationError):self.command()
        with self.security.store.database() as db:
            for table in ('commands','allocation_plans','allocation_reservations'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_application_terminal_state_cannot_bypass_unreleased_ledger_reservation(self):
        result=self.admitted();self.finish(result['trade_id'],filled=False)
        with self.assertRaises(ApplicationError) as error:self.command('PUT')
        self.assertEqual(error.exception.code,'PRIOR_ALLOCATION_RECONCILIATION_REQUIRED')
        self.assertEqual(len(self.held()),1)

    def test_atomic_concurrent_capital_reservation(self):
        self.authorize()
        def attempt(_):
            try:return self.command(identity=str(uuid.uuid4()),epoch=0)
            except ApplicationError:return None
        with ThreadPoolExecutor(max_workers=6) as workers:results=list(workers.map(attempt,range(6)))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(len(self.held()),1);self.assertEqual(self.provider.calls,1)

    def test_wrong_account_environment_generation_and_unproven_exposure_rejected(self):
        self.authorize()
        for change in (lambda e:replace(e,binding=replace(e.binding,account='DU9999999')),
                       lambda e:replace(e,binding=replace(e.binding,environment='LIVE')),
                       lambda e:replace(e,generation=2),lambda e:replace(e,exposure=replace(e.exposure,complete=False)),
                       lambda e:replace(e,exposure=replace(e.exposure,excludes_local_reservations=False))):
            self.provider.change=change
            with self.assertRaises(ApplicationError):self.command(epoch=0)

    def test_emergency_during_snapshot_rejects_plan_and_leaves_no_reservation(self):
        self.authorize()
        def emergency(data):
            self.authority.exit_all(self.service_token,'allocation-race')
            return data
        self.provider.change=emergency
        with self.assertRaises(ApplicationError):self.command()
        self.assertEqual(self.held(),[])

    def test_funded_targets_scheduler_limits_and_partial_fills(self):
        result,engine,fake=self.bound()
        self.assertEqual(Decimal(engine.portfolio['budget']),127750)
        actions=self.schedule(result,engine);self.assertEqual(sum(a.get('quantity',0) for a in actions),125)
        self.dispatch(result,engine,fake,actions);fake.match(capacity=2)
        self.assertTrue(fake.reconcile()['matched'])
        self.assertEqual(sum(engine.positions().values()),10)
        self.assertEqual(sum(engine.reservations('BUY',cid) for cid in engine.positions()),115)
        self.assertEqual(self.held()[0]['state'],'HELD')
        with self.assertRaises(ApplicationError):self.schedule(result,engine)
        self.assertTrue(engine.portfolio['halted'])
        self.assertEqual(len(engine.state['intents']),5)
        fake.match();self.assertTrue(fake.reconcile()['matched'])
        self.assertEqual(sum(engine.positions().values()),125)
        with self.assertRaises(ApplicationError):self.schedule(result,engine)

    def test_original_arrival_and_reservation_prevent_price_chasing_or_growth(self):
        result,engine,fake=self.bound()
        data=alloc_fixtures.evidence()
        for quote in data.quotes:
            if str(quote.option.contract.con_id) in engine.portfolio['targets']:
                changed=replace(quote,quote_id=quote.quote_id+':jump',option=replace(quote.option,bid=10.2,ask=10.3))
                engine.observe_liquidity(fake.generation,fake.event(),changed)
        # The mandatory hard gate now rejects before scheduler planning, while
        # retaining the original no-chase/fence/reservation guarantees.
        with self.assertRaises(ApplicationError) as denied:self.schedule(result,engine)
        self.assertEqual(denied.exception.code,'HARD_EXIT_CAPACITY_ADMISSION_REJECTED')
        self.assertTrue(engine.portfolio['halted'])
        self.assertFalse(any(i.get('side')=='BUY' for i in engine.state['intents'].values()))
        self.assertEqual(sum(t['quantity'] for t in engine.portfolio['targets'].values()),125)
        self.assertEqual(Decimal(self.held()[0]['capital']),127750)

    def test_lower_quotes_never_add_to_partially_affordable_plan(self):
        self.boundary=AllocationBoundary(alloc_fixtures.allocation_policy('15330'),self.provider)
        self.app=UnifiedApplication(self.security,self.authority,self.broker,self.boundary)
        result,engine,fake=self.bound()
        for quote in alloc_fixtures.evidence().quotes:
            if str(quote.option.contract.con_id) in engine.portfolio['targets']:
                engine.observe_liquidity(fake.generation,fake.event(),replace(quote,quote_id='lower',option=replace(quote.option,bid=8.9,ask=9.)))
        actions=self.schedule(result,engine)
        self.assertEqual(sum(a.get('quantity',0) for a in actions),15)
        self.assertEqual(sum(t['quantity'] for t in engine.portfolio['targets'].values()),15)

    def test_expired_grant_cannot_dispatch_pending_entries(self):
        result,engine,fake=self.bound();actions=self.schedule(result,engine)
        self.now+=181;fake.advance(181)
        with self.assertRaises(ApplicationError):self.boundary.simulate_dispatch(self.app,self.session.token,self.session.csrf_token,result['trade_id'],engine,[a['intent_id'] for a in actions],alloc_fixtures.evidence().account)
        self.assertTrue(engine.portfolio['halted']);self.assertTrue(all(i['order_id'] is None for i in engine.state['intents'].values()))
        self.assertEqual(self.held()[0]['state'],'HELD')

    def test_dashboard_expiry_preserves_independent_protection_and_reservations(self):
        result,engine,fake=self.bound();self.dispatch(result,engine,fake,self.schedule(result,engine));fake.match(capacity=1)
        self.assertTrue(fake.reconcile()['matched']);self.now+=901;fake.advance(901)
        with self.assertRaises(ApplicationError):self.schedule(result,engine)
        cancellations=IndependentProtectionService(self.authority,engine).reduction_actions(self.service_token)
        self.assertEqual(sum(a['type']=='CANCEL_REQUIRED' for a in cancellations),5)
        self.assertTrue(all(a.get('side')!='BUY' for a in cancellations))
        self.authority.require(self.service_token,'RECONCILE');self.authority.exit_all(self.service_token,'expired-dashboard')
        self.assertEqual(self.held()[0]['state'],'HELD');self.assertNotEqual(engine.status()['phase'],'FLAT_CONFIRMED')

    def test_emergency_after_plan_before_schedule_or_dispatch_blocks_new_buys(self):
        result,engine,fake=self.bound();actions=self.schedule(result,engine)
        self.app.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        with self.assertRaises(ApplicationError):self.boundary.simulate_dispatch(self.app,self.session.token,self.session.csrf_token,result['trade_id'],engine,[a['intent_id'] for a in actions],alloc_fixtures.evidence().account)
        self.assertTrue(engine.portfolio['emergency']);self.assertTrue(engine.portfolio['halted'])
        self.assertTrue(all(i['order_id'] is None for i in engine.state['intents'].values()))
        self.assertNotEqual(engine.status()['phase'],'FLAT_CONFIRMED')

    def test_journal_binding_prevents_duplicate_simulated_portfolios_and_is_durable(self):
        result,engine,fake=self.bound();other,other_fake=self.engine('other.sqlite')
        with self.assertRaises(ApplicationError):self.boundary.bind_engine(self.app,self.session.token,self.session.csrf_token,result['trade_id'],other,alloc_fixtures.evidence().account)
        self.security=MortificatioSecurity(self.root/'foundation.sqlite',self.security.policy,self.security.application_policy,lambda:self.now)
        self.app=UnifiedApplication(self.security,self.authority,self.broker,AllocationBoundary(alloc_fixtures.allocation_policy(),self.provider))
        self.assertEqual(self.app.direction_command(self.session.token,self.session.csrf_token,TradeCommand(result['allocation']['command_id'],'CALL',0))['allocation'],result['allocation'])
        self.assertEqual(self.held()[0]['state'],'HELD')

    def test_cancel_intent_does_not_release_capital_without_terminal_broker_evidence(self):
        result,engine,fake=self.bound();actions=self.schedule(result,engine);self.dispatch(result,engine,fake,actions)
        engine.trigger_exit('cancel-entry','STRATEGY_EXIT')
        cancels=engine.schedule(fake.generation)
        self.assertTrue(any(a['type']=='CANCEL_REQUIRED' for a in cancels))
        with self.assertRaises(ApplicationError):self.boundary.release_reconciled(self.app,self.service_token,result['trade_id'],engine)
        self.assertEqual(self.held()[0]['state'],'HELD')
        fake.cancel_actions(cancels);self.assertTrue(fake.reconcile()['matched'])
        self.finish(result['trade_id'],filled=False)
        self.assertEqual(self.boundary.release_reconciled(self.app,self.service_token,result['trade_id'],engine)['state'],'RELEASED')
        self.assertTrue(engine.portfolio['halted'])

    def test_missing_commissions_and_late_fills_hold_cash_until_flat(self):
        result,engine,fake=self.bound();ids=self.dispatch(result,engine,fake,self.schedule(result,engine))
        execution=fake.execution(ids[0],qty=1,commission=False)
        self.assertFalse(fake.reconcile()['matched'])
        with self.assertRaises(PaperBrokerError):self.boundary.release_reconciled(self.app,self.service_token,result['trade_id'],engine)
        fake.commission(execution.exec_id);self.assertTrue(fake.reconcile()['matched'])
        engine.trigger_exit('stop','INITIAL_STOP')
        cancels=engine.schedule(fake.generation);fake.cancel_actions(cancels)
        fake.execution(ids[1],qty=1)
        fake.order(ids[1],'Cancelled')
        self.assertTrue(fake.reconcile()['matched'])
        with self.assertRaises(ApplicationError):self.boundary.release_reconciled(self.app,self.service_token,result['trade_id'],engine)
        self.assertEqual(self.held()[0]['state'],'HELD');self.assertTrue(all(q>=0 for q in engine.positions().values()))

    def test_policy_and_plan_tampering_cannot_reallocate_reserved_cash(self):
        result=self.admitted()
        with self.security.store.database() as db:
            db.execute("UPDATE allocation_plans SET payload='{}' WHERE trade_id=?",(result['trade_id'],))
        with self.assertRaises(ApplicationError):self.command(identity=result['allocation']['command_id'],epoch=0)
        self.assertEqual(self.held()[0]['state'],'HELD')

    def test_no_network_or_broker_mutation_available(self):
        with patch('socket.socket',side_effect=AssertionError('offline only')):
            result,engine,fake=self.bound();self.schedule(result,engine)
            self.assertFalse(engine.status()['submission_enabled'])
            self.assertFalse(result['allocation']['submission_enabled'])

    def test_restart_requires_reconciliation_and_reauthorization_never_resubmits(self):
        result,engine,fake=self.bound();ids=self.dispatch(result,engine,fake,self.schedule(result,engine))
        fake.match(capacity=1);self.assertTrue(fake.reconcile()['matched'])
        path=engine.path;engine.close();self.engines.remove(engine)
        recovered=OfflinePortfolioLedger(path,approval(),policy());self.engines.append(recovered)
        fake.ledger=recovered;recovered.wall_clock=lambda:fake.wall;recovered.monotonic_clock=lambda:fake.mono
        fake.start();self.broker.generation=fake.generation
        self.assertEqual(recovered.portfolio['allocation']['trade_id'],result['trade_id'])
        self.assertEqual(len(recovered.state['intents']),len(ids))
        with self.assertRaises(ApplicationError):self.schedule(result,recovered)
        self.assertTrue(fake.reconcile()['matched'])
        self.assertTrue(recovered.portfolio['halted']);self.assertEqual(self.held()[0]['state'],'HELD')
        self.assertTrue(all(i['order_id'] is not None for i in recovered.state['intents'].values()))

    def test_capital_reserve_and_external_risk_rechecked_before_simulated_dispatch(self):
        result,engine,fake=self.bound();actions=self.schedule(result,engine)
        self.provider.change=lambda e:replace(e,exposure=replace(e.exposure,stop_risk='10000'))
        with self.assertRaises(ApplicationError):self.boundary.simulate_dispatch(self.app,self.session.token,self.session.csrf_token,result['trade_id'],engine,[a['intent_id'] for a in actions],alloc_fixtures.evidence().account)
        self.assertTrue(engine.portfolio['halted']);self.assertEqual(self.held()[0]['state'],'HELD')
        self.assertTrue(all(i['order_id'] is None for i in engine.state['intents'].values()))

    def test_buying_power_drop_cannot_consume_required_cash_reserve(self):
        self.boundary=AllocationBoundary(alloc_fixtures.allocation_policy(cash_reserve='10000'),self.provider)
        self.app=UnifiedApplication(self.security,self.authority,self.broker,self.boundary)
        result,engine,fake=self.bound();actions=self.schedule(result,engine)
        changed=replace(alloc_fixtures.evidence().account,available_funds='130000')
        self.provider.change=lambda e:replace(e,account=changed)
        with self.assertRaises(ApplicationError):self.boundary.simulate_dispatch(self.app,self.session.token,self.session.csrf_token,result['trade_id'],engine,[a['intent_id'] for a in actions],changed)
        self.assertTrue(engine.portfolio['halted'])

    def test_crash_between_binding_and_configuration_cannot_clone_plan(self):
        result=self.admitted();engine,fake=self.engine()
        original=engine.configure
        with patch.object(engine,'configure',side_effect=OSError('simulated process failure')):
            with self.assertRaises(OSError):self.boundary.bind_engine(self.app,self.session.token,self.session.csrf_token,result['trade_id'],engine,alloc_fixtures.evidence().account)
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM allocation_bindings').fetchone()[0],1)
        other,_=self.engine('other.sqlite')
        with self.assertRaises(ApplicationError):self.boundary.bind_engine(self.app,self.session.token,self.session.csrf_token,result['trade_id'],other,alloc_fixtures.evidence().account)
        self.assertFalse(engine.portfolio['targets'])
        self.boundary.bind_engine(self.app,self.session.token,self.session.csrf_token,result['trade_id'],engine,alloc_fixtures.evidence().account)
        self.assertTrue(engine.portfolio['targets'])

    def test_reconciled_cancellation_releases_reservation_only_once_and_next_plan_uses_cash(self):
        result,engine,fake=self.bound()
        self.finish(result['trade_id'],filled=False)
        self.assertEqual(self.held()[0]['state'],'HELD')
        self.boundary.release_reconciled(self.app,self.service_token,result['trade_id'],engine)
        self.boundary.release_reconciled(self.app,self.service_token,result['trade_id'],engine)
        following=self.command('PUT')
        self.assertEqual(following['allocation']['total_contracts'],125)
        self.assertEqual(self.held()[0]['state'],'RELEASED')
        self.assertTrue(engine.portfolio['halted'])

    def test_process_death_after_admission_preserves_plan_and_capital_reservation(self):
        self.authorize();identity=str(uuid.uuid4())
        script='''
import json,os,sys
from pathlib import Path
from mortificatio_auth import MortificatioSecurity,SecurityPolicy
from mortificatio_models import ApplicationPolicy,TradeCommand
from mortificatio_protection import ProtectionAuthority
from executor_application import UnifiedApplication
from executor_application_test import FixtureBrokerBoundary
from executor_allocation_test import allocation_policy,BINDING
from executor_allocation_service import AllocationBoundary
from executor_allocation_service_test import FixtureAllocationProvider
root=Path(sys.argv[1]);clock=lambda:float(sys.argv[2]);credentials=json.load(sys.stdin)
security=MortificatioSecurity(root/'foundation.sqlite',SecurityPolicy('mortificatio.test','https://mortificatio.test'),ApplicationPolicy(BINDING),clock)
app=UnifiedApplication(security,ProtectionAuthority(root/'protection.sqlite',BINDING),FixtureBrokerBoundary(clock,BINDING),AllocationBoundary(allocation_policy(),FixtureAllocationProvider()))
app.direction_command(credentials['session'],credentials['csrf'],TradeCommand(sys.argv[3],'CALL',0))
os._exit(79)
'''
        result=subprocess.run([sys.executable,'-B','-c',script,str(self.root),str(self.now),identity],
            input=json.dumps({'session':self.session.token,'csrf':self.session.csrf_token}),text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,79,result.stderr)
        duplicate=self.command(identity=identity,epoch=0)
        self.assertEqual(duplicate['result'],'DUPLICATE');self.assertEqual(duplicate['allocation']['total_contracts'],125)
        self.assertEqual(len(self.held()),1);self.assertEqual(self.provider.calls,0)

    def test_emergency_partial_entry_late_fills_independent_liquidation_and_release(self):
        result,engine,fake=self.bound();ids=self.dispatch(result,engine,fake,self.schedule(result,engine))
        fake.match(capacity=4);self.assertTrue(fake.reconcile()['matched'])
        self.app.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        worker=IndependentProtectionService(self.authority,engine)
        cancellations=worker.reduction_actions(self.service_token)
        self.assertEqual(sum(a['type']=='CANCEL_REQUIRED' for a in cancellations),5)
        self.assertEqual(self.held()[0]['state'],'HELD')
        fake.cancel_actions(cancellations)
        fake.execution(ids[0],qty=2);fake.order(ids[0],'Cancelled');self.assertTrue(fake.reconcile()['matched'])
        exits=worker.reduction_actions(self.service_token)
        self.assertTrue(all(a.get('side')!='BUY' for a in exits))
        fake.dispatch(exits);fake.match(side='SELL');self.assertTrue(fake.reconcile()['matched'])
        self.assertEqual(engine.status()['phase'],'FLAT_CONFIRMED')
        self.finish(result['trade_id'],filled=True)
        self.boundary.release_reconciled(self.app,self.service_token,result['trade_id'],engine)
        self.assertEqual(self.held()[0]['state'],'RELEASED')
        self.assertTrue(self.authority.emergency.blocked());self.assertFalse(engine.positions())
        with self.assertRaises(ApplicationError):self.command('PUT')


if __name__=='__main__':unittest.main()
