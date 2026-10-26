"""Phase 6 adversarial acceptance. Synthetic evidence; outbound sockets forbidden."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import random
import sqlite3
import stat
import unittest
import uuid
from unittest.mock import patch

import executor_dashboard_test as fixtures
from executor_application_api import SESSION_COOKIE
from mortificatio_auth import MortificatioSecurity
from mortificatio_models import ApplicationError, AccountBinding, ApplicationPolicy
from mortificatio_protection import IndependentProtectionService
from simulation_evidence import SPXObservation, FeedStatus


class AcceptanceTest(unittest.TestCase):
    # Reuse real verification and lifecycle fixtures without inheriting their tests.
    for _name in ('cleanup_lifecycle','enroll','login','grant','authorize','wall','mono',
                  'verify','enter','engine','orders','fill','last','restart','request','json','bootstrap'):
        locals()[_name]=getattr(fixtures.DashboardTest,_name)

    def setUp(self):
        for name in ('socket.socket.connect','socket.create_connection'):
            guard=patch(name,side_effect=AssertionError('Outbound socket forbidden in acceptance'))
            guard.start();self.addCleanup(guard.stop)
        fixtures.DashboardTest.setUp(self)

    def count_orders(self):
        with self.fixture.db() as db:return db.execute('SELECT count(*) FROM fixture_orders').fetchone()[0]

    def test_forged_tokens_and_service_capability_are_not_dashboard_credentials(self):
        for identity in ('x'*43,self.service_token,self.session.csrf_token):
            for path in ('/api/status','/api/history','/auth/session'):
                response=self.request(path,extra={'Cookie':SESSION_COOKIE+'='+identity,'X-Mortificatio-Session':'bootstrap'})
                self.assertEqual(response.status,401,(path,response.status))
        self.assertEqual(self.count_orders(),0)

    def test_origin_csrf_and_forwarded_header_attack_matrix(self):
        for extra in ({'Origin':'null'},{'Origin':'https://evil.test'},
                      {'Origin':'https://mortificatio.test.evil.test'},
                      {'Host':'evil.test','X-Forwarded-Host':'mortificatio.test'},
                      {'Sec-Fetch-Site':'cross-site'},{'Sec-Fetch-Site':'same-site'},
                      {'X-CSRF-Token':'x'*43}):
            with self.subTest(extra=extra):
                response=self.request('/api/exit-all','POST',{'request_id':str(uuid.uuid4())},extra=extra)
                self.assertNotEqual(response.status,200)
        self.assertFalse(self.authority.emergency.blocked())
        self.assertEqual(self.request('/api/status',secure=False,extra={'X-Forwarded-Proto':'https'}).status,403)

    def test_login_proof_cannot_be_replayed_as_trading_authorization(self):
        options=self.security.begin_login('127.0.0.1')
        assertion=self.authenticator.assertion(options['public_key'])
        session=self.security.complete_login(options['ceremony_token'],assertion,'127.0.0.1')
        step=self.lifecycle.begin_trading_authorization(session.token,session.csrf_token,'127.0.0.1')
        with self.assertRaises(ApplicationError):
            self.lifecycle.finish_trading_authorization(session.token,session.csrf_token,
                step['ceremony_token'],assertion,'127.0.0.1')
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM grants WHERE revoked=0').fetchone()[0],0)
        self.assertEqual(self.count_orders(),0)

    def test_account_environment_and_user_ownership_fail_closed(self):
        result=self.enter()
        # Fault injection into synthetic central ownership metadata, not an HTTP capability.
        with self.security.store.database() as db:
            original=dict(db.execute('SELECT * FROM commands').fetchone())
            for field,value in (('account','DU9999999'),('environment','LIVE'),('user_id',str(uuid.uuid4()))):
                db.execute('UPDATE commands SET '+field+'=?',(value,))
                self.assertEqual(self.request('/api/commands/'+original['command_id']).status,404)
                self.assertEqual(self.request('/api/history/'+result['trade_id']).status,404)
                db.execute('UPDATE commands SET '+field+'=?',(original[field],))
        other=MortificatioSecurity(self.root/'other-account.sqlite',self.security.policy,
            ApplicationPolicy(AccountBinding('DU9999999','PAPER','risk-v1')),lambda:self.now)
        with self.assertRaises(ApplicationError):other.session_status(self.session.token)
        with self.assertRaises(ApplicationError):
            MortificatioSecurity(self.security.store.path,self.security.policy,other.application_policy,lambda:self.now)

    def test_concurrent_opposite_commands_reserve_one_trade_and_retry_never_dispatches(self):
        self.authorize();identity=[str(uuid.uuid4()) for _ in range(8)]
        def submit(index):
            return self.request('/api/commands','POST',dict(command_id=identity[index],
                direction='CALL' if index%2 else 'PUT',entry_epoch=0))
        with ThreadPoolExecutor(max_workers=8) as pool:responses=list(pool.map(submit,range(8)))
        accepted=[self.json(r) for r in responses if r.status==200]
        self.assertEqual(len(accepted),1)
        count=self.count_orders();self.assertGreater(count,0)
        with self.security.store.database() as db:command=dict(db.execute('SELECT * FROM commands').fetchone())
        for _ in range(5):
            response=self.request('/api/commands','POST',{k:command[k] for k in ('command_id','direction','entry_epoch')})
            self.assertEqual(self.json(response)['result'],'DUPLICATE')
        self.assertEqual(self.count_orders(),count)
        with self.security.store.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM allocation_reservations WHERE state='HELD'").fetchone()[0],1)

    def test_randomized_manual_overrides_and_privilege_escalation_are_rejected(self):
        rng=random.Random(61006)
        for _ in range(80):
            field=rng.choice(('quantity','strike','allocation','account','environment','submission_enabled','broker_evidence','service_token'))
            command=dict(command_id=str(uuid.uuid4()),direction=rng.choice(('CALL','PUT')),entry_epoch=0)
            command[field]=rng.choice((0,25,125,'LIVE',True,None))
            self.assertEqual(self.request('/api/commands','POST',command).status,404)
        for path in ('/api/enable-live','/api/enable-paper','/api/orders','/api/withdraw','/api/settings/accounts'):
            self.assertNotEqual(self.request(path,'POST',{}).status,200)
        self.assertEqual(self.count_orders(),0)

    def test_service_cannot_authorize_entry_or_bind_a_buy_as_a_reduction(self):
        result=self.enter();engine=self.engine(result)
        service=IndependentProtectionService(self.authority,engine)
        for action in ('BUY','ACQUIRE','TRADING_AUTHORIZE','DISPATCH'):
            with self.assertRaises(ApplicationError):self.authority.require(self.service_token,action)
        self.assertFalse(any(a.get('side')=='BUY' for a in service.reduction_actions(self.service_token)))
        with self.assertRaises(ApplicationError):
            service.simulate_reduction_bindings(self.service_token,[self.orders(result)[0]['intent_id']],9999)

    def test_revocation_and_emergency_with_partial_exposure_preserve_reservations(self):
        result=self.enter();self.fill(result,1);count=self.count_orders()
        self.security.logout(self.session.token,self.session.csrf_token)
        self.assertEqual(self.request('/api/status').status,401)
        self.assertEqual(self.request('/api/exit-all','POST',{'request_id':str(uuid.uuid4())}).status,401)
        self.authority.exit_all(self.service_token,str(uuid.uuid4()))
        self.lifecycle.supervise(self.service_token)
        engine=self.engine(result)
        self.assertTrue(engine.portfolio['halted']);self.assertTrue(engine.portfolio['emergency'])
        self.assertEqual(sum(engine.positions().values()),5)
        self.assertLessEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT state FROM allocation_reservations').fetchone()[0],'HELD')
        self.assertGreaterEqual(self.count_orders(),count)
        self.restart();self.lifecycle.supervise(self.service_token)
        self.assertTrue(self.engine(result).portfolio['halted'])
        self.assertEqual(sum(self.engine(result).positions().values()),5)

    def test_source_skew_and_missing_greeks_prevent_allocation(self):
        self.authorize();original=self.provider.snapshot
        for kind in ('skew','missing_source','missing_greeks','stale'):
            def snapshot(direction):
                data=original(direction);quotes=[]
                for quote in data.quotes:
                    if kind=='skew':quote=replace(quote,greeks_source_time=self.wall()-timedelta(seconds=.251))
                    elif kind=='missing_source':quote=replace(quote,option=replace(quote.option,source_time=None))
                    elif kind=='missing_greeks':quote=replace(quote,greeks=None)
                    else:quote=replace(quote,option=replace(quote.option,source_time=self.wall()-timedelta(seconds=2)))
                    quotes.append(quote)
                return replace(data,quotes=tuple(quotes))
            with patch.object(self.provider,'snapshot',snapshot):
                response=self.request('/api/commands','POST',dict(command_id=str(uuid.uuid4()),direction='CALL',entry_epoch=0))
                self.assertNotEqual(response.status,200,kind)
        self.assertEqual(self.count_orders(),0)
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT count(*) FROM allocation_reservations').fetchone()[0],0)

    def test_put_stops_and_unlimited_trailing_during_partial_entry(self):
        direction='PUT';result=self.enter(direction);self.fill(result,1);engine=self.engine(result)
        for favorable in (4,4.9,5,10,20,50):
            self.now+=.01
            self.lifecycle.observe_spx(self.service_token,result['trade_id'],str(uuid.uuid4()),
                SPXObservation(6000-favorable,FeedStatus.LIVE,self.wall(),self.mono()))
            self.assertIsNone(engine.portfolio['exit_reason'])
            if favorable>=5:self.assertTrue(engine.portfolio['ride'])
        self.assertEqual(Decimal(engine.portfolio['peak']),Decimal('5950'))
        self.now+=.01
        self.lifecycle.observe_spx(self.service_token,result['trade_id'],str(uuid.uuid4()),
            SPXObservation(5953,FeedStatus.LIVE,self.wall(),self.mono()))
        self.assertIsNotNone(engine.portfolio['exit_reason'])
        self.assertTrue(engine.portfolio['halted'])
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
        for cid,quantity in engine.positions().items():
            average,stop=engine._premium_stop(cid)
            self.assertEqual(average-stop,Decimal('3.25'))
            self.assertLessEqual(engine.reservations('SELL',cid),quantity)

    def test_audit_redaction_integrity_and_private_sidecars(self):
        self.authorize();self.bootstrap()
        rows=json.dumps(self.security.store.security_events())
        for value in (self.session.token,self.session.csrf_token,self.service_token):self.assertNotIn(value,rows)
        for file in self.root.glob('*.sqlite*'):
            self.assertEqual(stat.S_IMODE(file.stat().st_mode)&0o077,0,file.name)
        with self.security.store.database() as db:db.execute("UPDATE security_audit SET record='{}' WHERE sequence=1")
        self.assertEqual(self.request('/api/status').status,503)
        self.assertEqual(self.count_orders(),0)

    def test_authentication_rate_limit_never_gates_independent_emergency(self):
        result=self.enter();self.fill(result,1)
        for _ in range(35):
            try:self.security.begin_login('127.0.0.1')
            except ApplicationError:pass
        with self.assertRaises(ApplicationError) as denial:self.security.begin_login('127.0.0.1')
        self.assertEqual(denial.exception.status,429)
        response=self.request('/api/exit-all','POST',{'request_id':str(uuid.uuid4())})
        self.assertEqual(response.status,200)
        self.assertTrue(self.authority.emergency.blocked())
        self.assertTrue(self.engine(result).portfolio['emergency'])

    def test_foundation_outage_preserves_protective_processing_and_independent_latch(self):
        result=self.enter();self.fill(result,1);engine=self.engine(result)
        prior=engine.portfolio['peak'];count=self.count_orders()
        with patch.object(self.security.store,'database',side_effect=sqlite3.OperationalError('injected unavailable foundation')):
            status=self.lifecycle.observe_spx(self.service_token,result['trade_id'],'outage-spx',
                SPXObservation(6005,FeedStatus.LIVE,self.wall(),self.mono()))
            self.assertEqual(Decimal(engine.portfolio['peak']),Decimal('6005'))
            self.assertTrue(status['entry_fenced'])
            self.authority.exit_all(self.service_token,str(uuid.uuid4()))
            actions=IndependentProtectionService(self.authority,engine).reduction_actions(self.service_token)
            self.assertFalse(any(a.get('side')=='BUY' for a in actions))
        self.assertTrue(self.authority.emergency.blocked())
        self.assertEqual(sum(engine.positions().values()),5)
        self.assertGreaterEqual(self.count_orders(),count)
        self.assertNotEqual(self.last(result)['state'],'FLAT_CONFIRMED')

    def test_false_empty_broker_snapshot_cannot_finalize_or_release_owned_exposure(self):
        result=self.enter();self.fill(result,1);original=self.fixture.snapshot
        with patch.object(self.fixture,'snapshot',lambda generation:replace(original(generation),positions=())):
            self.lifecycle.supervise(self.service_token)
            self.assertNotEqual(self.last(result)['state'],'FLAT_CONFIRMED')
            detail=self.json(self.request('/api/history/'+result['trade_id']))
            self.assertEqual(detail['status'],'PENDING_RECONCILIATION')
            self.assertIsNone(detail['totals'])
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT state FROM allocation_reservations').fetchone()[0],'HELD')
        self.assertEqual(sum(self.engine(result).positions().values()),5)


if __name__=='__main__':unittest.main()
