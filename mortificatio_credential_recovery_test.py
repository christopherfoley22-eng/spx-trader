"""Offline credential recovery adversaries with real WebAuthn signatures; no broker."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest
from unittest.mock import patch
import cbor2
from webauthn.helpers import bytes_to_base64url
from webauthn import base64url_to_bytes

import mortificatio_auth_test as fixtures
from mortificatio_auth import MortificatioSecurity
from mortificatio_models import ApplicationError
from mortificatio_test_authenticator import OfflineAuthenticator

PEER=fixtures.PEER


class RecoveryTest(unittest.TestCase):
    for _name in ('setUp','enroll','login','grant'):locals()[_name]=getattr(fixtures.AuthTest,_name)

    def add(self,role='ADD_RECOVERY',session=None):
        session=session or self.login()
        proof=self.security.begin_key_change(session.token,session.csrf_token,PEER,role)
        registration=self.security.finish_key_change(session.token,session.csrf_token,proof['ceremony_token'],
            self.authenticator.assertion(proof['public_key']),PEER)
        key=OfflineAuthenticator()
        response=key.registration(registration['public_key'])
        result=self.security.finish_key_registration(registration['ceremony_token'],response,PEER,session.token,session.csrf_token)
        return key,result

    def recovery_registration(self,key):
        options=self.security.begin_recovery(PEER)
        return self.security.finish_recovery_proof(options['ceremony_token'],key.assertion(options['public_key']),PEER)

    def test_backup_registration_requires_fresh_proof_and_revokes_sessions_and_grants(self):
        session=self.login();self.grant(session)
        key,result=self.add('ADD_LOGIN',session)
        self.assertFalse(result['trading_authorized'])
        with self.assertRaises(ApplicationError):self.security.session_status(session.token)
        self.authenticator=key;fresh=self.login()
        with self.assertRaises(ApplicationError):
            self.security.store.transact(lambda db,now:self.security.require_grant(db,now,self.security.require_session(db,now,fresh.token),1))

    def test_stolen_session_and_csrf_without_key_cannot_add_an_authenticator(self):
        session=self.login();options=self.security.begin_key_change(session.token,session.csrf_token,PEER,'ADD_LOGIN')
        impostor=OfflineAuthenticator();impostor.credential=self.authenticator.credential;impostor.handle=self.authenticator.handle
        with self.assertRaises(ApplicationError):
            self.security.finish_key_change(session.token,session.csrf_token,options['ceremony_token'],impostor.assertion(options['public_key']),PEER)
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT count(*) FROM credentials').fetchone()[0],1)

    def test_missing_session_and_cross_session_management_are_rejected(self):
        with self.assertRaises(ApplicationError):self.security.begin_key_change('x'*43,'x'*43,PEER,'ADD_LOGIN')
        first=self.login();other=self.login()
        options=self.security.begin_key_change(first.token,first.csrf_token,PEER,'ADD_LOGIN')
        with self.assertRaises(ApplicationError):
            self.security.finish_key_change(other.token,other.csrf_token,options['ceremony_token'],self.authenticator.assertion(options['public_key']),PEER)

    def test_recovery_key_cannot_login_or_authorize_trading(self):
        key,_=self.add();options=self.security.begin_login(PEER)
        with self.assertRaises(ApplicationError):self.security.complete_login(options['ceremony_token'],key.assertion(options['public_key']),PEER)
        session=self.login();options=self.security.begin_step_up(session.token,session.csrf_token,PEER,'TRADING_AUTHORIZE',1)
        with self.assertRaises(ApplicationError):
            self.security.complete_step_up(session.token,session.csrf_token,options['ceremony_token'],key.assertion(options['public_key']),PEER,'TRADING_AUTHORIZE',1)

    def test_lost_login_recovery_revokes_old_keys_and_stolen_sessions_and_requires_new_login(self):
        key,_=self.add();stolen=self.login();self.grant(stolen)
        registration=self.recovery_registration(key);replacement=OfflineAuthenticator()
        response=replacement.registration(registration['public_key'])
        result=self.security.finish_key_registration(registration['ceremony_token'],response,PEER,recovery=True)
        self.assertEqual(result['result'],'RECOVERED_LOGIN_REQUIRED');self.assertFalse(result['submission_enabled'])
        with self.assertRaises(ApplicationError):self.security.session_status(stolen.token)
        with self.assertRaises(ApplicationError):self.login()
        self.authenticator=replacement;session=self.login()
        with self.assertRaises(ApplicationError):
            self.security.store.transact(lambda db,now:self.security.require_grant(db,now,self.security.require_session(db,now,session.token),1))

    def test_normal_login_key_cannot_impersonate_recovery_authority(self):
        self.add();options=self.security.begin_recovery(PEER)
        with self.assertRaises(ApplicationError):
            self.security.finish_recovery_proof(options['ceremony_token'],self.authenticator.assertion(options['public_key']),PEER)

    def test_recovery_proof_replay_and_registration_replay_are_rejected(self):
        key,_=self.add();options=self.security.begin_recovery(PEER);proof=key.assertion(options['public_key'])
        registration=self.security.finish_recovery_proof(options['ceremony_token'],proof,PEER)
        with self.assertRaises(ApplicationError):self.security.finish_recovery_proof(options['ceremony_token'],proof,PEER)
        new=OfflineAuthenticator();response=new.registration(registration['public_key'])
        self.security.finish_key_registration(registration['ceremony_token'],response,PEER,recovery=True)
        with self.assertRaises(ApplicationError):self.security.finish_key_registration(registration['ceremony_token'],response,PEER,recovery=True)

    def test_wrong_origin_missing_uv_expired_proof_and_unenrolled_recovery_fail(self):
        with self.assertRaises(ApplicationError):self.security.begin_recovery(PEER)
        key,_=self.add()
        for changes in ({'origin':'https://evil.test'},{'flags':1}):
            options=self.security.begin_recovery(PEER)
            with self.assertRaises(ApplicationError):self.security.finish_recovery_proof(options['ceremony_token'],key.assertion(options['public_key'],**changes),PEER)
            self.now+=61
        options=self.security.begin_recovery(PEER);self.now+=61
        with self.assertRaises(ApplicationError):self.security.finish_recovery_proof(options['ceremony_token'],key.assertion(options['public_key']),PEER)

    def test_replacement_registration_requires_uv_and_exact_origin(self):
        key,_=self.add()
        for changes in ({'flags':0x41},{'origin':'https://evil.test'}):
            registration=self.recovery_registration(key);replacement=OfflineAuthenticator()
            with self.assertRaises(ApplicationError):
                self.security.finish_key_registration(registration['ceremony_token'],replacement.registration(registration['public_key'],**changes),PEER,recovery=True)
            self.now+=61

    def test_compromised_authenticator_revocation_requires_distinct_surviving_key(self):
        with self.assertRaises(ApplicationError):
            session=self.login();self.security.begin_key_change(session.token,session.csrf_token,PEER,'REVOKE',self.security.credentials(session.token)['credentials'][0]['credential_id'])
        original=self.authenticator;backup,_=self.add('ADD_LOGIN');session=self.login()
        target=self.security.credentials(session.token)['credentials'][0]['credential_id']
        options=self.security.begin_key_change(session.token,session.csrf_token,PEER,'REVOKE',target)
        result=self.security.finish_key_change(session.token,session.csrf_token,options['ceremony_token'],backup.assertion(options['public_key']),PEER)
        self.assertIn('REVOKED',result['result'])
        with self.assertRaises(ApplicationError):self.login()
        self.authenticator=backup;self.assertTrue(self.security.session_status(self.login().token)['authenticated'])

    def test_inflight_recovery_invalidated_by_credential_revocation(self):
        key,_=self.add();registration=self.recovery_registration(key);session=self.login()
        target=next(k['credential_id'] for k in self.security.credentials(session.token)['credentials'] if k['role']=='RECOVERY')
        options=self.security.begin_key_change(session.token,session.csrf_token,PEER,'REVOKE',target)
        self.security.finish_key_change(session.token,session.csrf_token,options['ceremony_token'],self.authenticator.assertion(options['public_key']),PEER)
        replacement=OfflineAuthenticator()
        with self.assertRaises(ApplicationError):self.security.finish_key_registration(registration['ceremony_token'],replacement.registration(registration['public_key']),PEER,recovery=True)

    def test_restart_keeps_roles_revocations_and_pending_single_use_recovery(self):
        key,_=self.add();registration=self.recovery_registration(key)
        self.security=MortificatioSecurity(self.security.store.path,self.security.policy,self.security.application_policy,lambda:self.now)
        replacement=OfflineAuthenticator();result=self.security.finish_key_registration(registration['ceremony_token'],replacement.registration(registration['public_key']),PEER,recovery=True)
        self.assertEqual(result['result'],'RECOVERED_LOGIN_REQUIRED')

    def test_recovery_failure_is_atomic_and_does_not_revoke_working_identity(self):
        key,_=self.add();registration=self.recovery_registration(key);replacement=OfflineAuthenticator()
        with patch.object(self.security,'_revoke_sessions',side_effect=OSError('injected disk failure')):
            with self.assertRaises(OSError):self.security.finish_key_registration(registration['ceremony_token'],replacement.registration(registration['public_key']),PEER,recovery=True)
        self.assertTrue(self.security.session_status(self.login().token)['authenticated'])
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT count(*) FROM credentials').fetchone()[0],2)

    def test_unenrolled_bootstrap_retry_is_local_only_and_cannot_reset_established_identity(self):
        with self.assertRaises(ApplicationError):self.security.renew_unenrolled_bootstrap()
        fresh=MortificatioSecurity(self.root/'unenrolled.sqlite',self.security.policy,self.security.application_policy,lambda:self.now)
        old=fresh.provision_owner_bootstrap();fresh.begin_registration(old,PEER)
        new=fresh.renew_unenrolled_bootstrap()
        with self.assertRaises(ApplicationError):fresh.begin_registration(old,PEER)
        self.assertIn('public_key',fresh.begin_registration(new,PEER))

    def test_non_string_attestation_formats_are_rejected_before_verification(self):
        for malformed in (None,0,['none'],{'fmt':'none'}):
            session=self.login()
            options=self.security.begin_key_change(session.token,session.csrf_token,PEER,'ADD_LOGIN')
            registration=self.security.finish_key_change(session.token,session.csrf_token,options['ceremony_token'],self.authenticator.assertion(options['public_key']),PEER)
            key=OfflineAuthenticator();response=key.registration(registration['public_key'])
            decoded=cbor2.loads(base64url_to_bytes(response['response']['attestationObject']));decoded['fmt']=malformed
            response['response']['attestationObject']=bytes_to_base64url(cbor2.dumps(decoded))
            with self.assertRaises(ApplicationError):self.security.finish_key_registration(registration['ceremony_token'],response,PEER,session.token,session.csrf_token)
            self.now+=61


if __name__=='__main__':unittest.main()
