"""In-memory TLS-adapter contract tests; no sockets or broker connections."""
import json
import stat
import unittest
import uuid
from dataclasses import replace
from http.cookies import SimpleCookie

import executor_application_test as fixtures
from executor_application import UnifiedApplication
from executor_application_api import FoundationAPI,APIRequest,SESSION_COOKIE,FLOW_COOKIE


class APITest(unittest.TestCase):
    enroll=fixtures.ApplicationTest.enroll
    login=fixtures.ApplicationTest.login
    grant=fixtures.ApplicationTest.grant

    def setUp(self):
        fixtures.ApplicationTest.setUp(self)
        self.api=FoundationAPI(self.app)
        self.jar={SESSION_COOKIE:self.session.token}

    def request(self,path,body=None,method='POST',headers=None,secure=True):
        fields={'Host':'mortificatio.test','Origin':'https://mortificatio.test',
                'Content-Type':'application/json','X-CSRF-Token':self.session.csrf_token,
                'Cookie':'; '.join(k+'='+v for k,v in self.jar.items())}
        fields.update(headers or {})
        raw=body if isinstance(body,bytes) else json.dumps(body or {}).encode()
        result=self.api.dispatch(APIRequest(method,path,fields,raw,secure,'127.0.0.1'))
        for key,value in result.headers:
            if key=='Set-Cookie':
                cookie=SimpleCookie();cookie.load(value)
                for name,item in cookie.items():
                    if item['max-age']=='0':self.jar.pop(name,None)
                    else:self.jar[name]=item.value
        return result

    def test_real_signed_login_cookie_security_no_automatic_trading_grant(self):
        self.jar={}
        options=self.request('/auth/login/options')
        self.assertEqual(options.status,200)
        self.assertNotIn('ceremony_token',options.body)
        response=self.request('/auth/login/complete',{'credential':self.authenticator.assertion(options.body['public_key'])})
        self.assertEqual(response.status,200)
        self.assertFalse(response.body['trading_authorized']);self.assertFalse(response.body['submission_enabled'])
        self.assertNotIn(self.jar[SESSION_COOKIE],json.dumps(response.body))
        for key,value in response.headers:
            if key=='Set-Cookie':
                for flag in ('Secure','HttpOnly','SameSite=Strict','Path=/'):self.assertIn(flag,value)
        self.assertEqual(dict(response.headers)['Cache-Control'],'no-store')

    def test_tls_host_origin_fetch_site_cannot_be_forged_with_forwarding_headers(self):
        self.assertEqual(self.request('/api/status',method='GET',secure=False,headers={'X-Forwarded-Proto':'https'}).status,403)
        for headers in ({'Host':'evil.test'},{'Origin':'https://evil.test'},{'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.request('/api/status',method='GET',headers=headers).status,403)
        self.assertEqual(self.request('/api/status',method='GET').status,200)

    def test_anonymous_sensitive_requests_and_local_bootstrap_provisioning_denied(self):
        self.jar={}
        for path,body,method in (('/api/status',{},'GET'),('/api/commands',self.command(),'POST'),
                                 ('/api/exit-all',{'request_id':str(uuid.uuid4())},'POST'),('/api/settings/risk',{},'POST')):
            self.assertNotEqual(self.request(path,body,method).status,200)
        for path in ('/auth/provision-owner','/auth/provision-service','/auth/recover'):
            self.assertEqual(self.request(path).status,404)

    def command(self):return {'command_id':str(uuid.uuid4()),'direction':'CALL','entry_epoch':0}

    def test_csrf_required_for_sensitive_actions_and_configuration_changes_disabled(self):
        for path,body in (('/auth/trading/options',{}),('/auth/logout',{}),('/api/commands',self.command()),
                          ('/api/exit-all',{'request_id':str(uuid.uuid4())}),('/api/settings/risk',{})):
            self.assertEqual(self.request(path,body,headers={'X-CSRF-Token':''}).status,401)
        self.assertEqual(self.request('/api/settings/risk').status,403)

    def test_trading_step_up_and_idempotent_commands_never_dispatch(self):
        options=self.request('/auth/trading/options')
        response=self.request('/auth/trading/complete',{'credential':self.authenticator.assertion(options.body['public_key'])})
        self.assertEqual(response.status,200)
        command=self.command();first=self.request('/api/commands',command);second=self.request('/api/commands',command)
        self.assertEqual(first.body['state'],'QUEUED_OFFLINE');self.assertFalse(first.body['submission_enabled'])
        self.assertEqual(second.body['result'],'DUPLICATE')
        self.assertEqual(first.body['trade_id'],second.body['trade_id'])

    def test_manual_strikes_quantities_and_unknown_endpoints_rejected(self):
        for key in ('quantity','strike','allocation','account','environment','broker_evidence'):
            body=self.command();body[key]=25
            self.assertEqual(self.request('/api/commands',body).status,404)
        for path in ('/api/orders','/api/enable-paper','/api/enable-live','/api/withdraw','/api/transfer'):
            self.assertEqual(self.request(path).status,404)

    def test_duplicate_json_oversize_nonfinite_and_cookie_identity_rejected(self):
        self.assertEqual(self.request('/api/commands',b'{"direction":"CALL","direction":"PUT"}').status,400)
        self.assertEqual(self.request('/api/commands',b'{"entry_epoch":NaN}').status,400)
        self.assertEqual(self.request('/api/commands',b' '*65537).status,400)
        self.assertEqual(self.request('/api/status',method='GET',headers={'Cookie':SESSION_COOKIE+'='+self.session.token+'; '+SESSION_COOKIE+'='+self.session.token}).status,401)

    def test_logout_revokes_session_and_replayed_cookie(self):
        saved=self.jar[SESSION_COOKIE]
        self.assertEqual(self.request('/auth/logout').status,200)
        self.jar[SESSION_COOKIE]=saved
        self.assertEqual(self.request('/api/status',method='GET').status,401)

    def test_emergency_remains_available_without_grant_or_broker_and_is_durable(self):
        self.broker.changes={'connected':False,'market_passed':False}
        identity=str(uuid.uuid4())
        for _ in range(2):
            result=self.request('/api/exit-all',{'request_id':identity})
            self.assertEqual(result.status,200);self.assertEqual(result.body['state'],'PENDING_RECONCILIATION')
        self.assertTrue(self.authority.emergency.blocked())

    def test_default_broker_boundary_denies_trading_authorization(self):
        self.api=FoundationAPI(UnifiedApplication(self.security,self.authority))
        self.assertNotEqual(self.request('/auth/trading/options').status,200)

    def test_flow_cannot_be_used_for_different_security_purpose(self):
        options=self.request('/auth/login/options')
        response=self.request('/auth/trading/complete',{'credential':self.authenticator.assertion(options.body['public_key'])})
        self.assertNotEqual(response.status,200)
        login=self.request('/auth/login/complete',{'credential':self.authenticator.assertion(options.body['public_key'])})
        self.assertEqual(login.status,200)
        self.assertFalse(login.body['trading_authorized'])

    def test_database_and_live_wal_are_private_before_first_sqlite_write(self):
        with self.security.store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE foundation_meta SET last_clock=last_clock')
            for path in self.root.iterdir():
                if path.is_file():self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600,path.name)


if __name__=='__main__':unittest.main()
