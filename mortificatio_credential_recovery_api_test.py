"""Credential recovery API identity, cookies, origin and CSRF; no transport or broker."""
import json
import unittest
import executor_application_api_test as fixtures
from executor_application_api import SESSION_COOKIE
from mortificatio_test_authenticator import OfflineAuthenticator


class RecoveryAPITest(unittest.TestCase):
    for _name in ('setUp','enroll','login','grant','request','command'):
        locals()[_name]=getattr(fixtures.APITest,_name)

    def add_recovery(self):
        options=self.request('/auth/keys/options',{'action':'ADD_RECOVERY'})
        self.assertEqual(options.status,200,options.body)
        registration=self.request('/auth/keys/prove',{'credential':self.authenticator.assertion(options.body['public_key'])})
        self.assertEqual(registration.status,200,registration.body)
        key=OfflineAuthenticator()
        result=self.request('/auth/keys/register',{'credential':key.registration(registration.body['public_key'])})
        self.assertEqual(result.status,200,result.body)
        return key,result

    def test_management_requires_dashboard_origin_and_csrf(self):
        for headers in ({'Cookie':''},{'Origin':'https://evil.test'},{'X-CSRF-Token':'x'*43}):
            self.assertNotEqual(self.request('/auth/keys/options',{'action':'ADD_LOGIN'},headers=headers).status,200)
        self.assertEqual(self.request('/auth/credentials',method='GET',headers={'Cookie':''}).status,401)

    def test_add_recovery_revokes_cookie_and_never_returns_session_grant_or_flow_secrets(self):
        key,result=self.add_recovery()
        self.assertNotIn(SESSION_COOKIE,self.jar)
        self.assertFalse(result.body['trading_authorized'])
        self.assertEqual(self.request('/api/status',method='GET').status,401)
        raw=json.dumps(result.body)
        for secret in (self.session.token,self.service_token):self.assertNotIn(secret,raw)
        self.assertNotIn('ceremony_token',raw)

    def test_complete_recovery_requires_new_login_and_no_broker_permission_is_created(self):
        key,_=self.add_recovery()
        options=self.request('/auth/recovery/options')
        registration=self.request('/auth/recovery/prove',{'credential':key.assertion(options.body['public_key'])})
        replacement=OfflineAuthenticator()
        result=self.request('/auth/recovery/register',{'credential':replacement.registration(registration.body['public_key'])})
        self.assertEqual(result.status,200,result.body)
        self.assertEqual(result.body['result'],'RECOVERED_LOGIN_REQUIRED')
        self.assertNotIn(SESSION_COOKIE,self.jar)
        self.assertEqual(self.request('/api/commands',self.command()).status,401)
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM grants WHERE revoked=0').fetchone()[0],0)

    def test_recovery_origin_mutation_and_payload_authority_injection_are_rejected(self):
        for headers in ({'Origin':'null'},{'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.request('/auth/recovery/options',headers=headers).status,403)
        for path in ('/auth/recovery/options','/auth/keys/options'):
            self.assertEqual(self.request(path,{'action':'ADD_LOGIN','account':'DU9999999','trading_authorized':True}).status,404)

    def test_no_local_bootstrap_reset_or_password_recovery_route(self):
        for path in ('/auth/renew-unenrolled-bootstrap','/auth/reset-owner','/auth/recover-password'):
            self.assertEqual(self.request(path).status,404)


if __name__=='__main__':unittest.main()
