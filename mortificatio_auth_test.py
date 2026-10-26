"""Real WebAuthn verification against synthetic authenticator signatures. Offline."""
from dataclasses import replace
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from mortificatio_auth import MortificatioSecurity,SecurityPolicy
from mortificatio_models import ApplicationPolicy,AccountBinding,ApplicationError
from mortificatio_test_authenticator import OfflineAuthenticator

NOW=datetime(2026,10,23,14,30,tzinfo=timezone.utc).timestamp()
BINDING=AccountBinding('DU1234567','PAPER','risk-v1')
PEER='127.0.0.1'


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=NOW
        self.security=MortificatioSecurity(self.root/'foundation.sqlite',SecurityPolicy('mortificatio.test','https://mortificatio.test'),ApplicationPolicy(BINDING),lambda:self.now)
        self.authenticator=OfflineAuthenticator()
        self.enroll()

    def enroll(self):
        bootstrap=self.security.provision_owner_bootstrap()
        options=self.security.begin_registration(bootstrap,PEER)
        self.assertEqual(options['public_key']['authenticatorSelection']['userVerification'],'required')
        response=self.authenticator.registration(options['public_key'])
        self.assertEqual(self.security.complete_registration(options['ceremony_token'],response,PEER)['result'],'ENROLLED')

    def login(self,security=None,old=None,counter=None):
        security=security or self.security
        options=security.begin_login(PEER)
        self.assertEqual(options['public_key']['userVerification'],'required')
        response=self.authenticator.assertion(options['public_key'],counter=counter)
        return security.complete_login(options['ceremony_token'],response,PEER,old)

    def grant(self,session,generation=1):
        options=self.security.begin_step_up(session.token,session.csrf_token,PEER,'TRADING_AUTHORIZE',generation)
        return self.security.complete_step_up(session.token,session.csrf_token,options['ceremony_token'],self.authenticator.assertion(options['public_key']),PEER,'TRADING_AUTHORIZE',generation)

    def test_login_uses_real_signature_and_does_not_authorize_trading(self):
        session=self.login()
        self.assertTrue(self.security.session_status(session.token)['authenticated'])
        with self.assertRaises(ApplicationError):
            self.security.store.transact(lambda db,now:self.security.require_grant(db,now,self.security.require_session(db,now,session.token),1))
        self.assertNotIn(session.token,repr(session))
        self.grant(session)
        self.assertFalse(self.security.session_status(session.token)['submission_enabled'])

    def test_forged_signature_wrong_rp_origin_challenge_and_missing_user_verification(self):
        for changes in ({'origin':'https://evil.test'},{'challenge':'b3RoZXItY2hhbGxlbmdl'},{'rp_id':'evil.test'},
                        {'flags':1},{'flags':4},{'crossOrigin':True},{'topOrigin':'https://evil.test'}):
            options=self.security.begin_login(PEER)
            response=self.authenticator.assertion(options['public_key'],**changes)
            with self.subTest(changes=changes),self.assertRaises(ApplicationError):
                self.security.complete_login(options['ceremony_token'],response,PEER)
            self.now+=61
        options=self.security.begin_login(PEER)
        attacker=OfflineAuthenticator();attacker.credential=self.authenticator.credential;attacker.handle=self.authenticator.handle
        with self.assertRaises(ApplicationError):
            self.security.complete_login(options['ceremony_token'],attacker.assertion(options['public_key']),PEER)

    def test_login_challenge_single_use_expiry_and_failed_response_consumption(self):
        options=self.security.begin_login(PEER);response=self.authenticator.assertion(options['public_key'])
        session=self.security.complete_login(options['ceremony_token'],response,PEER)
        with self.assertRaises(ApplicationError):self.security.complete_login(options['ceremony_token'],response,PEER)
        options=self.security.begin_login(PEER);self.now+=61
        with self.assertRaises(ApplicationError):self.security.complete_login(options['ceremony_token'],self.authenticator.assertion(options['public_key']),PEER)
        options=self.security.begin_login(PEER)
        bad=self.authenticator.assertion(options['public_key'],origin='https://evil.test')
        with self.assertRaises(ApplicationError):self.security.complete_login(options['ceremony_token'],bad,PEER)
        with self.assertRaises(ApplicationError):self.security.complete_login(options['ceremony_token'],self.authenticator.assertion(options['public_key']),PEER)

    def test_session_rotation_blocks_fixation_and_revokes_prior_grants(self):
        old=self.login();self.grant(old)
        fresh=self.login(old=old.token)
        self.assertNotEqual(old.token,fresh.token)
        with self.assertRaises(ApplicationError):self.security.session_status(old.token)
        self.assertTrue(self.security.session_status(fresh.token)['authenticated'])

    def test_absolute_idle_expiration_and_logout_revoke_sessions(self):
        session=self.login();self.grant(session)
        self.now+=301
        with self.assertRaises(ApplicationError):self.security.session_status(session.token)
        self.assertTrue(any(e['kind']=='SESSION_EXPIRED' for e in self.security.store.security_events()))
        session=self.login();self.security.logout(session.token,session.csrf_token)
        with self.assertRaises(ApplicationError):self.security.session_status(session.token)
        session=self.login()
        for _ in range(4):self.now+=200;self.security.session_status(session.token)
        self.now+=101
        with self.assertRaises(ApplicationError):self.security.session_status(session.token)

    def test_csrf_missing_and_incorrect_cannot_mutate(self):
        session=self.login()
        for csrf in (None,'bad','x'*43):
            with self.assertRaises(ApplicationError):self.security.logout(session.token,csrf)
            with self.assertRaises(ApplicationError):self.security.begin_step_up(session.token,csrf,PEER,'TRADING_AUTHORIZE',1)
        self.assertTrue(self.security.session_status(session.token)['authenticated'])

    def test_separate_fresh_assertion_and_context_binding_for_trading_grants(self):
        session=self.login();other=self.login()
        options=self.security.begin_step_up(session.token,session.csrf_token,PEER,'TRADING_AUTHORIZE',1)
        response=self.authenticator.assertion(options['public_key'])
        with self.assertRaises(ApplicationError):
            self.security.complete_step_up(other.token,other.csrf_token,options['ceremony_token'],response,PEER,'TRADING_AUTHORIZE',1)
        with self.assertRaises(ApplicationError):
            self.security.complete_step_up(session.token,session.csrf_token,options['ceremony_token'],response,PEER,'TRADING_AUTHORIZE',2)
        self.grant(session)
        def validate(db,now,generation):return self.security.require_grant(db,now,self.security.require_session(db,now,session.token),generation)
        with self.assertRaises(ApplicationError):self.security.store.transact(lambda db,now:validate(db,now,2))
        self.now+=181
        with self.assertRaises(ApplicationError):self.security.store.transact(lambda db,now:validate(db,now,1))

    def test_counterless_passkeys_use_challenge_replay_protection(self):
        session=self.login(counter=0)
        fresh=self.login(counter=0)
        self.assertNotEqual(session.token,fresh.token)
        self.assertTrue(self.security.session_status(fresh.token)['authenticated'])

    def test_positive_authenticator_counter_rollback_is_rejected(self):
        self.login(counter=5)
        options=self.security.begin_login(PEER)
        with self.assertRaises(ApplicationError):
            self.security.complete_login(options['ceremony_token'],self.authenticator.assertion(options['public_key'],counter=4),PEER)

    def test_wrong_user_handle_unknown_credential_and_id_mismatch_rejected(self):
        for kind in ('handle','id','rawId'):
            options=self.security.begin_login(PEER);response=self.authenticator.assertion(options['public_key'])
            if kind=='handle':response['response']['userHandle']='d3Jvbmc'
            else:response[kind]='d3Jvbmc'
            with self.assertRaises(ApplicationError):self.security.complete_login(options['ceremony_token'],response,PEER)

    def test_brute_force_rate_limit_survives_restart(self):
        for _ in range(5):
            options=self.security.begin_login(PEER)
            with self.assertRaises(ApplicationError):self.security.complete_login(options['ceremony_token'],self.authenticator.assertion(options['public_key'],flags=1),PEER)
        self.security=MortificatioSecurity(self.root/'foundation.sqlite',self.security.policy,self.security.application_policy,lambda:self.now)
        with self.assertRaises(ApplicationError) as error:self.security.begin_login(PEER)
        self.assertEqual(error.exception.status,429)
        self.now+=61
        self.assertTrue(self.security.begin_login(PEER))

    def test_revoke_all_requires_fresh_key_proof_and_survives_restart(self):
        first=self.login();second=self.login();self.grant(second)
        opts=self.security.begin_step_up(first.token,first.csrf_token,PEER,'REVOKE_SESSIONS')
        self.security.complete_step_up(first.token,first.csrf_token,opts['ceremony_token'],self.authenticator.assertion(opts['public_key']),PEER,'REVOKE_SESSIONS')
        for session in (first,second):
            with self.assertRaises(ApplicationError):self.security.session_status(session.token)
        self.security=MortificatioSecurity(self.root/'foundation.sqlite',self.security.policy,self.security.application_policy,lambda:self.now)
        with self.assertRaises(ApplicationError):self.security.session_status(first.token)

    def test_one_assertion_cannot_create_two_sessions_concurrently(self):
        opts=self.security.begin_login(PEER);response=self.authenticator.assertion(opts['public_key'])
        def finish(_):
            try:return self.security.complete_login(opts['ceremony_token'],response,PEER)
            except ApplicationError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(finish,range(2)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_no_tokens_or_assertions_in_audit_or_database_and_clock_rollback_fails(self):
        session=self.login();self.grant(session)
        with self.security.store.database() as db:
            rows=''.join(str(tuple(r)) for table in ('security_audit','sessions','grants','challenges') for r in db.execute('SELECT * FROM '+table))
        self.assertNotIn(session.token,rows);self.assertNotIn(session.csrf_token,rows)
        self.now-=1
        with self.assertRaises(ApplicationError):self.security.session_status(session.token)

    def test_security_audit_tampering_and_policy_changes_fail_closed(self):
        self.login()
        with sqlite3.connect(self.root/'foundation.sqlite') as db:db.execute("UPDATE security_audit SET record='{}' WHERE sequence=1")
        with self.assertRaises(ApplicationError):self.security.begin_login(PEER)
        with self.assertRaises(ApplicationError):
            MortificatioSecurity(self.root/'foundation.sqlite',self.security.policy,replace(self.security.application_policy,max_trades_per_day=1),lambda:self.now)

    def test_no_automatic_second_owner_or_insecure_origin(self):
        with self.assertRaises(ApplicationError):self.security.provision_owner_bootstrap()
        with self.assertRaises(ApplicationError):
            MortificatioSecurity(self.root/'bad.sqlite',replace(self.security.policy,origin='http://mortificatio.test'),self.security.application_policy)

    def test_registration_rejects_wrong_origin_missing_uv_and_consumes_challenge(self):
        for index,changes in enumerate(({'origin':'https://evil.test'},{'flags':65})):
            security=MortificatioSecurity(self.root/('enrollment-{}.sqlite'.format(index)),self.security.policy,self.security.application_policy,lambda:self.now)
            bootstrap=security.provision_owner_bootstrap()
            options=security.begin_registration(bootstrap,PEER)
            key=OfflineAuthenticator()
            response=key.registration(options['public_key'],**changes)
            with self.assertRaises(ApplicationError):security.complete_registration(options['ceremony_token'],response,PEER)
            with self.assertRaises(ApplicationError):security.complete_registration(options['ceremony_token'],key.registration(options['public_key']),PEER)
            with security.store.database() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM credentials').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],0)


if __name__=='__main__':unittest.main()
