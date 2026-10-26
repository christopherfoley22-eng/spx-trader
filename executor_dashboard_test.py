"""Offline authenticated web boundary tests; no broker connection or deployment."""
from dataclasses import replace
from html.parser import HTMLParser
import json
from pathlib import Path
import unittest
import uuid

import executor_lifecycle_test as fixtures
from executor_dashboard_web import DashboardWebApplication
from executor_application_api import APIRequest,SESSION_COOKIE
from simulation_evidence import SPXObservation,FeedStatus


class Buttons(HTMLParser):
    def __init__(self):super().__init__();self.actions=[];self.inputs=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='button' and 'data-trading-action' in attrs:self.actions.append(attrs['data-trading-action'])
        if tag in ('input','select'):self.inputs.append(attrs.get('id',''))


class DashboardTest(unittest.TestCase):
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

    def request(self,path,method='GET',body=None,extra=None,secure=True,cookie=True):
        headers={'Host':'mortificatio.test','Origin':'https://mortificatio.test','Sec-Fetch-Site':'same-origin','Content-Type':'application/json'}
        if cookie:headers.update({'Cookie':SESSION_COOKIE+'='+self.session.token,'X-CSRF-Token':self.session.csrf_token})
        headers.update(extra or {})
        return DashboardWebApplication(self.lifecycle).dispatch(APIRequest(method,path,headers,json.dumps(body or {}).encode(),secure,'127.0.0.1'))

    def json(self,response):return json.loads(response.body)

    def bootstrap(self):
        response=self.request('/auth/session',extra={'X-Mortificatio-Session':'bootstrap'})
        if response.status==200:self.session=replace(self.session,csrf_token=self.json(response)['csrf_token'])
        return response

    def test_exactly_three_trading_actions_and_no_manual_allocation_controls(self):
        response=self.request('/',cookie=False);parser=Buttons();parser.feed(response.body.decode())
        self.assertEqual(sorted(parser.actions),['CALL','EXIT_ALL','PUT'])
        self.assertFalse(any('strike' in name or 'quantity' in name or 'allocation' in name for name in parser.inputs))

    def test_public_shell_has_no_private_account_trade_or_auth_secrets(self):
        response=self.request('/history',cookie=False)
        self.assertEqual(response.status,200)
        for secret in (self.session.token,self.session.csrf_token,self.service_token,'DU1234567'):
            self.assertNotIn(secret,response.body.decode())
        self.assertEqual(self.request('/api/status',cookie=False).status,401)
        self.assertEqual(self.request('/api/history',cookie=False).status,401)

    def test_static_security_headers_and_traversal_protection(self):
        for path in ('/','/assets/dashboard.js','/assets/dashboard.css','/assets/mountains.svg'):
            response=self.request(path,cookie=False);self.assertEqual(response.status,200)
            headers=dict(response.headers);self.assertEqual(headers['Cache-Control'],'no-store')
            self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
            self.assertNotIn("'unsafe-inline'",headers['Content-Security-Policy'])
            self.assertNotIn('Access-Control-Allow-Origin',headers)
        self.assertEqual(self.request('/assets/../mortificatio_auth.py').status,404)
        self.assertEqual(self.request('/requirements-security.lock').status,404)

    def test_csrf_resume_requires_current_authenticated_same_origin_context(self):
        self.assertEqual(self.request('/auth/session').status,403)
        self.assertEqual(self.request('/auth/session',extra={'X-Mortificatio-Session':'bootstrap','Sec-Fetch-Site':'cross-site'}).status,403)
        self.assertEqual(self.request('/auth/session',extra={'X-Mortificatio-Session':'bootstrap'},cookie=False).status,401)
        self.assertEqual(self.request('/auth/session',extra={'X-Mortificatio-Session':'bootstrap','X-Forwarded-Proto':'https'},secure=False).status,403)

    def test_refresh_issues_bound_csrf_and_preserves_scope_and_separate_grant(self):
        before=self.session.csrf_token;first=self.bootstrap();data=self.json(first)
        self.assertFalse(data['trading_authorization']['active'])
        self.assertFalse(data['submission_enabled']);self.assertFalse(data['real_ibkr_authenticated'])
        self.assertNotEqual(data['csrf_token'],before)
        self.authorize();second=self.json(self.bootstrap())
        self.assertTrue(second['trading_authorization']['active'])
        self.assertEqual(second['command_scope'],data['command_scope'])
        failed=self.request('/api/exit-all','POST',{'request_id':str(uuid.uuid4())},extra={'X-CSRF-Token':'invalid-csrf-token-value000'})
        self.assertIn(failed.status,(401,403))
        self.security.store.transact(lambda db,now:self.security.require_session(db,now,self.session.token,before,write=True))
        self.authority.require(self.service_token,'OBSERVE_RISK')

    def test_browser_refresh_does_not_invalidate_already_authorized_acquisition(self):
        self.bootstrap();result=self.enter();self.fill(result,1)
        before=self.engine(result).portfolio['halted']
        self.assertTrue(before)  # Native protection is not yet broker-confirmed.
        for _ in range(5):self.bootstrap()
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(self.engine(result).portfolio['halted'],before)
        self.assertEqual(sum(self.engine(result).positions().values()),5)

    def test_refresh_tokens_are_hashed_session_bound_and_revoked_on_rotation(self):
        self.bootstrap();old_token=self.session.csrf_token
        with self.security.store.database() as db:
            stored=str([tuple(row) for row in db.execute('SELECT * FROM session_csrf_tokens')])
        self.assertNotIn(old_token,stored)
        previous=self.session.token;self.session=self.login(old=previous)
        response=self.request('/api/exit-all','POST',{'request_id':str(uuid.uuid4())},extra={'X-CSRF-Token':old_token})
        self.assertIn(response.status,(401,403));self.assertFalse(self.authority.emergency.blocked())

    def test_text_contrast_and_focus_support_are_present(self):
        def luminance(color):
            channels=[int(color[i:i+2],16)/255 for i in (0,2,4)]
            channels=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in channels]
            return sum(v*w for v,w in zip(channels,(.2126,.7152,.0722)))
        for foreground,background in (('eeeae0','171a18'),('b6baae','202420'),('d2c2a0','171a18'),('e5eee5','2d3b2f'),('f0ddd8','3c302b')):
            values=sorted((luminance(foreground),luminance(background)))
            self.assertGreaterEqual((values[1]+.05)/(values[0]+.05),4.5)
        css=(Path(__file__).with_name('dashboard')/'dashboard.css').read_text()
        self.assertIn(':focus-visible',css);self.assertIn('prefers-reduced-motion',css)

    def test_login_only_still_cannot_trade(self):
        self.bootstrap()
        response=self.request('/api/commands','POST',{'command_id':str(uuid.uuid4()),'direction':'CALL','entry_epoch':0})
        self.assertNotEqual(response.status,200)
        self.assertFalse(self.lifecycle.engines)

    def test_one_press_and_read_only_command_recovery_never_redrive(self):
        self.bootstrap();self.authorize();identity=str(uuid.uuid4())
        command={'command_id':identity,'direction':'CALL','entry_epoch':0}
        response=self.request('/api/commands','POST',command);self.assertEqual(response.status,200,response.body)
        result=self.json(response);self.assertEqual(len(result['allocation']['lines']),5)
        before=len(self.orders(result));epoch=self.core.status(self.session.token)['entry_epoch']
        self.bootstrap()
        for _ in range(3):
            recovered=self.request('/api/commands/'+identity);self.assertEqual(recovered.status,200)
            self.assertEqual(self.json(recovered)['trade_id'],result['trade_id'])
            self.assertNotIn('market_evidence',self.json(recovered)['allocation'])
        self.assertEqual(len(self.orders(result)),before);self.assertEqual(self.core.status(self.session.token)['entry_epoch'],epoch)

    def test_active_api_never_discloses_peak_or_prices(self):
        result=self.enter();self.fill(result,1)
        self.lifecycle.observe_spx(self.service_token,result['trade_id'],'private-peak',SPXObservation(6050.,FeedStatus.LIVE,self.wall(),self.mono()))
        response=self.request('/api/status');body=response.body.decode()
        for forbidden in ('favorable_spx_peak','trailing_threshold','weighted_average_premium','initial_premium_stop','"6050'):
            self.assertNotIn(forbidden,body)
        self.assertEqual(self.engine(result).portfolio['peak'],'6050.0')

    def test_broker_unavailable_does_not_look_authenticated_or_authorized(self):
        self.lifecycle.disconnect(self.service_token)
        data=self.json(self.bootstrap())
        self.assertFalse(data['broker_evidence_available']);self.assertEqual(data['broker_evidence_origin'],'UNAVAILABLE')
        self.assertFalse(data['trading_authorization']['active'])
        self.assertFalse(data['real_ibkr_authenticated'])

    def test_expired_dashboard_does_not_abandon_existing_exposure(self):
        result=self.enter();self.fill(result,1);self.now+=901
        self.assertEqual(self.bootstrap().status,401)
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(sum(self.engine(result).positions().values()),5)
        self.assertTrue(self.engine(result).portfolio['halted'])
        self.assertFalse(self.engine(result).status()['phase']=='FLAT_CONFIRMED')

    def test_emergency_is_not_gated_by_trading_grant_or_broker_health(self):
        self.bootstrap();self.lifecycle.disconnect(self.service_token)
        response=self.request('/api/exit-all','POST',{'request_id':str(uuid.uuid4())})
        self.assertEqual(response.status,200,response.body)
        self.assertTrue(self.authority.emergency.blocked())
        self.assertEqual(self.json(response)['state'],'PENDING_RECONCILIATION')

    def test_command_lookup_rejects_anonymous_and_unknown_context(self):
        result=self.enter();identity=result['allocation']['command_id']
        self.assertEqual(self.request('/api/commands/'+identity,cookie=False).status,401)
        self.assertEqual(self.request('/api/commands/'+str(uuid.uuid4())).status,404)
        self.assertEqual(self.request('/api/commands/'+identity+'?raw=true').status,400)

    def test_restart_recovers_command_without_new_bindings(self):
        result=self.enter();self.fill(result,1);count=len(self.orders(result));self.restart()
        self.bootstrap();response=self.request('/api/commands/'+result['allocation']['command_id'])
        self.assertEqual(response.status,200,response.body);self.assertEqual(len(self.orders(result)),count)
        self.assertEqual(self.json(response)['allocation']['total_contracts'],125)
        self.assertTrue(self.engine(result).portfolio['halted'])


if __name__=='__main__':unittest.main()
