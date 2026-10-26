"""Phase 1 application authorization and durable account command/frequency tests."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import unittest
import uuid

import mortificatio_auth_test as auth_fixtures
from mortificatio_auth import MortificatioSecurity
from mortificatio_models import (ApplicationError,TradeCommand,BrokerAdmissionEvidence,ManagedTradeEvidence,AccountBinding)
from mortificatio_protection import ProtectionAuthority,IndependentProtectionService
from executor_application import UnifiedApplication


class FixtureBrokerBoundary:
    def __init__(self,clock,binding):self.clock=clock;self.binding=binding;self.generation=1;self.changes={}
    def admission_evidence(self):
        now=datetime.fromtimestamp(self.clock(),timezone.utc)
        return replace(BrokerAdmissionEvidence(self.binding,self.generation,True,True,True,True,True,
                       now,now+timedelta(seconds=1),'SYNTHETIC_BROKER_PREFLIGHT'),**self.changes)


class ApplicationTest(unittest.TestCase):
    enroll=auth_fixtures.AuthTest.enroll
    login=auth_fixtures.AuthTest.login
    grant=auth_fixtures.AuthTest.grant

    def setUp(self):
        auth_fixtures.AuthTest.setUp(self)
        self.authority=ProtectionAuthority(self.root/'protection.sqlite',auth_fixtures.BINDING)
        self.service_token=self.authority.provision_local_service()
        self.broker=FixtureBrokerBoundary(lambda:self.now,auth_fixtures.BINDING)
        self.app=UnifiedApplication(self.security,self.authority,self.broker)
        self.session=self.login()

    def authorize(self):
        opts=self.app.begin_trading_authorization(self.session.token,self.session.csrf_token,auth_fixtures.PEER)
        return self.app.finish_trading_authorization(self.session.token,self.session.csrf_token,opts['ceremony_token'],
            self.authenticator.assertion(opts['public_key']),auth_fixtures.PEER)

    def command(self,direction='CALL',identity=None,epoch=None):
        if epoch is None:epoch=self.app.status(self.session.token)['entry_epoch']
        command=TradeCommand(identity or str(uuid.uuid4()),direction,epoch)
        return self.app.direction_command(self.session.token,self.session.csrf_token,command)

    def evidence(self,trade,revision=1,filled=True,quantity=0,orders=0,complete=True,**changes):
        stamp=datetime.fromtimestamp(self.now,timezone.utc) if filled else None
        return replace(ManagedTradeEvidence(trade,auth_fixtures.BINDING,1,revision,str(uuid.uuid4()),filled,stamp,
            quantity,orders,complete,complete,complete),**changes)

    def finish(self,trade,filled=True):
        return self.app.observe_managed_trade(self.service_token,self.evidence(trade,filled=filled))

    def test_login_only_cannot_admit_but_separate_fresh_authorization_can(self):
        with self.assertRaises(ApplicationError):self.command()
        self.authorize();result=self.command()
        self.assertEqual(result['result'],'ACCEPTED_OFFLINE')
        self.assertEqual(result['state'],'QUEUED_OFFLINE')
        self.assertFalse(result['submission_enabled'])
        self.assertFalse(self.app.status(self.session.token)['real_broker_verified'])

    def test_call_and_put_commands_have_no_quantity_or_strike_parameters(self):
        self.authorize()
        for direction in ('CALL','PUT'):
            result=self.command(direction);self.finish(result['trade_id'])
        with self.assertRaises(ApplicationError):self.command('SELL')
        with self.assertRaises(TypeError):TradeCommand(str(uuid.uuid4()),'CALL',0,quantity=25)

    def test_same_command_is_idempotent_after_restart_and_cannot_change_direction(self):
        self.authorize();identity=str(uuid.uuid4());first=self.command(identity=identity,epoch=0)
        restarted=MortificatioSecurity(self.root/'foundation.sqlite',self.security.policy,self.security.application_policy,lambda:self.now)
        app=UnifiedApplication(restarted,self.authority,self.broker)
        duplicate=app.direction_command(self.session.token,self.session.csrf_token,TradeCommand(identity,'CALL',0))
        self.assertEqual(duplicate['trade_id'],first['trade_id']);self.assertEqual(duplicate['result'],'DUPLICATE')
        with self.assertRaises(ApplicationError):app.direction_command(self.session.token,self.session.csrf_token,TradeCommand(identity,'PUT',0))
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT count(*) FROM commands').fetchone()[0],1)

    def test_concurrent_requests_from_separate_instances_reserve_one_trade(self):
        self.authorize()
        other_security=MortificatioSecurity(self.root/'foundation.sqlite',self.security.policy,self.security.application_policy,lambda:self.now)
        other=UnifiedApplication(other_security,self.authority,self.broker)
        def submit(index):
            try:return (self.app if index%2 else other).direction_command(self.session.token,self.session.csrf_token,TradeCommand(str(uuid.uuid4()),'CALL',0))
            except ApplicationError:return None
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(submit,range(8)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_new_identifier_with_old_epoch_cannot_repeat_completed_trade(self):
        self.authorize();first=self.command();self.finish(first['trade_id'])
        with self.assertRaises(ApplicationError):self.command(identity=str(uuid.uuid4()),epoch=0)
        self.assertEqual(self.app.status(self.session.token)['entry_epoch'],1)

    def test_account_frequency_survives_logout_relogin_restarts_and_new_sessions(self):
        self.authorize();first=self.command();self.finish(first['trade_id'])
        self.security.logout(self.session.token,self.session.csrf_token)
        self.session=self.login();self.authorize();second=self.command('PUT');self.finish(second['trade_id'])
        self.security=MortificatioSecurity(self.root/'foundation.sqlite',self.security.policy,self.security.application_policy,lambda:self.now)
        self.app=UnifiedApplication(self.security,self.authority,self.broker)
        with self.assertRaises(ApplicationError) as error:self.command()
        self.assertEqual(error.exception.code,'ACCOUNT_TRADE_FREQUENCY_LIMIT')

    def test_zero_fill_cancellation_releases_slot_only_with_complete_evidence(self):
        self.authorize();first=self.command()
        self.app.observe_managed_trade(self.service_token,self.evidence(first['trade_id'],filled=False,complete=False))
        with self.assertRaises(ApplicationError):self.command()
        self.app.observe_managed_trade(self.service_token,self.evidence(first['trade_id'],revision=2,filled=False))
        second=self.command('PUT');self.finish(second['trade_id'])
        third=self.command();self.finish(third['trade_id'])
        with self.assertRaises(ApplicationError):self.command()

    def test_incomplete_history_or_commissions_never_complete_or_release_trade(self):
        self.authorize();result=self.command()
        evidence=self.evidence(result['trade_id'],commissions_complete=False)
        recorded=self.app.observe_managed_trade(self.service_token,evidence)
        self.assertEqual(recorded['state'],'PENDING_RECONCILIATION')
        with self.assertRaises(ApplicationError):self.command()
        self.assertEqual(len(self.app.status(self.session.token)['pending_commands']),1)

    def test_late_exposure_reopens_account_recovery_instead_of_false_completion(self):
        self.authorize();first=self.command();self.finish(first['trade_id'])
        self.command('PUT')
        late=self.evidence(first['trade_id'],revision=2,quantity=1,complete=False)
        self.assertEqual(self.app.observe_managed_trade(self.service_token,late)['state'],'RECOVERY_REQUIRED')
        status=self.app.status(self.session.token)
        self.assertTrue(status['account_blocked']);self.assertEqual(len(status['pending_commands']),2)

    def test_lifecycle_duplicates_changes_stale_revision_and_wrong_account_are_rejected(self):
        self.authorize();first=self.command();evidence=self.evidence(first['trade_id'],quantity=1,complete=False)
        self.app.observe_managed_trade(self.service_token,evidence)
        self.assertEqual(self.app.observe_managed_trade(self.service_token,evidence)['result'],'DUPLICATE')
        for bad in (replace(evidence,position_quantity=2),replace(evidence,evidence_id=str(uuid.uuid4())),
                    replace(evidence,evidence_id=str(uuid.uuid4()),revision=2,binding=AccountBinding('DU9999999','PAPER','risk-v1'))):
            with self.assertRaises(ApplicationError):self.app.observe_managed_trade(self.service_token,bad)

    def test_missing_stale_live_wrong_account_and_risk_market_funds_fail_closed(self):
        self.authorize()
        cases=({'connected':False},{'account_verified':False},{'origin':'REAL_IBKR'},
               {'binding':replace(auth_fixtures.BINDING,account='DU9999999')},
               {'binding':replace(auth_fixtures.BINDING,environment='LIVE')},
               {'risk_passed':False},{'market_passed':False},{'buying_power_passed':False},
               {'observed_at':datetime.fromtimestamp(self.now-1.01,timezone.utc)})
        for change in cases:
            self.broker.changes=change
            with self.subTest(change=change),self.assertRaises(ApplicationError):self.command(epoch=0)
        self.broker.changes={}
        disabled=UnifiedApplication(self.security,self.authority)
        with self.assertRaises(ApplicationError):disabled.direction_command(self.session.token,self.session.csrf_token,TradeCommand(str(uuid.uuid4()),'CALL',0))
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT count(*) FROM commands').fetchone()[0],0)

    def test_reconnection_generation_requires_new_authorization(self):
        self.authorize();self.broker.generation=2
        with self.assertRaises(ApplicationError):self.command()
        self.authorize();self.assertEqual(self.command()['result'],'ACCEPTED_OFFLINE')

    def test_emergency_requires_login_and_csrf_but_not_grant_market_or_broker(self):
        self.broker.changes={'connected':False,'market_passed':False,'buying_power_passed':False}
        result=self.app.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        self.assertTrue(result['emergency']);self.assertEqual(result['state'],'PENDING_RECONCILIATION')
        with self.assertRaises(ApplicationError):self.app.emergency_exit_all(None,None,str(uuid.uuid4()))
        self.assertTrue(self.authority.emergency.blocked())

    def test_service_scope_cannot_login_create_grants_or_admit_entries(self):
        with self.assertRaises(ApplicationError):self.security.session_status(self.service_token)
        with self.assertRaises(ApplicationError):self.authority.require(self.service_token,'ENTRY')
        self.authority.require(self.service_token,'RECONCILE')
        self.authority.revoke_local_service(self.service_token)
        with self.assertRaises(ApplicationError):self.authority.require(self.service_token,'EXIT_ALL')

    def test_dashboard_expiry_does_not_revoke_protection_or_abandon_mock_position(self):
        import executor_portfolio_test as portfolio_fixtures
        from simulation_evidence import SPXObservation,FeedStatus
        fixture=portfolio_fixtures.PortfolioTest('test_single_strike_executor_and_live_boundaries_remain_unchanged')
        fixture.setUp()
        try:
            fixture.own();self.authorize();command=self.command()
            self.app.observe_managed_trade(self.service_token,self.evidence(command['trade_id'],quantity=125,complete=False))
            worker=IndependentProtectionService(self.authority,fixture.ledger)
            self.now+=901;fixture.fake.advance(901);fixture.quotes();self.assertTrue(fixture.fake.reconcile()['matched'])
            with self.assertRaises(ApplicationError):self.command()
            worker.observe_spx(self.service_token,'arm',SPXObservation(6005,FeedStatus.LIVE,fixture.fake.wall,fixture.fake.mono))
            worker.observe_spx(self.service_token,'reverse',SPXObservation(6002,FeedStatus.LIVE,fixture.fake.wall,fixture.fake.mono))
            actions=worker.reduction_actions(self.service_token)
            self.assertEqual(sum(a.get('quantity',0) for a in actions),125)
            self.assertTrue(all(a.get('side')!='BUY' for a in actions))
            self.assertTrue(self.app.observe_managed_trade(self.service_token,self.evidence(command['trade_id'],revision=2,quantity=125,complete=False)))
        finally:fixture.doCleanups()

    def test_pending_command_survives_actual_process_death_without_readmission(self):
        self.authorize();identity=str(uuid.uuid4());session=self.session
        script='''
import os,sys,json
from mortificatio_auth import MortificatioSecurity,SecurityPolicy
from mortificatio_models import ApplicationPolicy,AccountBinding,TradeCommand
from mortificatio_protection import ProtectionAuthority
from executor_application import UnifiedApplication
from executor_application_test import FixtureBrokerBoundary
binding=AccountBinding('DU1234567','PAPER','risk-v1');clock=lambda:float(sys.argv[4])
security=MortificatioSecurity(sys.argv[1],SecurityPolicy('mortificatio.test','https://mortificatio.test'),ApplicationPolicy(binding),clock)
authority=ProtectionAuthority(sys.argv[2],binding)
app=UnifiedApplication(security,authority,FixtureBrokerBoundary(clock,binding))
credentials=json.load(sys.stdin)
app.direction_command(credentials['session'],credentials['csrf'],TradeCommand(sys.argv[3],'CALL',0))
os._exit(73)
'''
        result=subprocess.run([sys.executable,'-B','-c',script,str(self.root/'foundation.sqlite'),str(self.root/'protection.sqlite'),identity,str(self.now)],input=json.dumps({'session':session.token,'csrf':session.csrf_token}),capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,73,result.stderr)
        result=self.command(identity=identity,epoch=0)
        self.assertEqual(result['result'],'DUPLICATE')
        with self.assertRaises(ApplicationError):self.command()

    def test_new_york_daily_frequency_reset_requires_new_login_and_grant(self):
        self.authorize()
        for direction in ('CALL','PUT'):
            trade=self.command(direction);self.finish(trade['trade_id'])
        self.now+=24*60*60
        with self.assertRaises(ApplicationError):self.command()
        self.session=self.login();self.authorize()
        self.assertEqual(self.command()['result'],'ACCEPTED_OFFLINE')

    def test_service_and_emergency_survive_dashboard_store_restart(self):
        identity='x'*120
        self.authority.exit_all(self.service_token,identity)
        restarted=ProtectionAuthority(self.root/'protection.sqlite',auth_fixtures.BINDING)
        restarted.require(self.service_token,'RECONCILE')
        self.assertEqual(restarted.exit_all(self.service_token,identity),'DUPLICATE')
        self.now+=901
        with self.assertRaises(ApplicationError):self.security.session_status(self.session.token)
        restarted.require(self.service_token,'EXIT_ALL')
        self.assertTrue(restarted.emergency.blocked())

    def test_emergency_fences_admission_even_if_independent_latch_write_fails(self):
        self.authorize()
        original=self.authority.emergency.request
        def interrupted(request_id):
            with self.assertRaises(ApplicationError) as error:self.command()
            self.assertEqual(error.exception.code,'ACCOUNT_RECOVERY_REQUIRED')
            raise OSError('simulated disk failure')
        self.authority.emergency.request=interrupted
        try:
            with self.assertRaises(OSError):self.app.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        finally:self.authority.emergency.request=original
        self.assertTrue(self.app.status(self.session.token)['account_blocked'])


if __name__=='__main__':unittest.main()
