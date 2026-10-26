"""Authenticated, read-only history APIs and active-trade allowlist tests."""
from dataclasses import replace
from decimal import Decimal
import json
import unittest
import uuid
from urllib.parse import urlencode

import executor_trade_history_test as fixtures
from executor_application_api import FoundationAPI,APIRequest,SESSION_COOKIE
from executor_blind_presentation import dashboard_response
from mortificatio_auth import MortificatioSecurity
from mortificatio_models import ApplicationError
from mortificatio_test_authenticator import OfflineAuthenticator
from simulation_evidence import SPXObservation,FeedStatus


class PresentationTest(unittest.TestCase):
    setUp=fixtures.HistoryTest.setUp
    cleanup_lifecycle=fixtures.HistoryTest.cleanup_lifecycle
    enroll=fixtures.HistoryTest.enroll
    login=fixtures.HistoryTest.login
    grant=fixtures.HistoryTest.grant
    authorize=fixtures.HistoryTest.authorize
    wall=fixtures.HistoryTest.wall
    mono=fixtures.HistoryTest.mono
    verify=fixtures.HistoryTest.verify
    enter=fixtures.HistoryTest.enter
    engine=fixtures.HistoryTest.engine
    orders=fixtures.HistoryTest.orders
    fill=fixtures.HistoryTest.fill
    last=fixtures.HistoryTest.last
    restart=fixtures.HistoryTest.restart
    one_strike=fixtures.HistoryTest.one_strike
    detail=fixtures.HistoryTest.detail
    exit_strategy=fixtures.HistoryTest.exit_strategy
    complete=fixtures.HistoryTest.complete

    def request(self,path,method='GET',body=None,token=None):
        headers={'Host':'mortificatio.test','Origin':'https://mortificatio.test','Cookie':SESSION_COOKIE+'='+(self.session.token if token is None else token),
            'X-CSRF-Token':self.session.csrf_token,'Content-Type':'application/json'}
        return FoundationAPI(self.lifecycle).dispatch(APIRequest(method,path,headers,json.dumps(body or {}).encode(),True,'127.0.0.1'))

    def assert_blind(self,value):
        forbidden={'favorable_spx_peak','peak','entry_spx','entry_price','exit_price','weighted_average_premium','initial_premium_stop',
            'weighted_average_entry','weighted_average_exit','early_protection_active','early_protection_threshold',
            'trailing_active','trailing_threshold','exit_reason','market_evidence','greeks','delta','execution_history',
            'commission_history','order_history','option_premium','estimated_premium_cost','estimated_fees','premium_cost','ask','bid'}
        def visit(node):
            if isinstance(node,dict):
                for key,item in node.items():
                    self.assertNotIn(key,forbidden,key)
                    if 'pnl' in key.lower():self.assertIsNone(item)
                    visit(item)
            elif isinstance(node,list):
                for item in node:visit(item)
        visit(value)

    def test_active_status_command_and_duplicate_never_expose_protection_milestones(self):
        self.authorize();command={'command_id':str(uuid.uuid4()),'direction':'CALL','entry_epoch':0}
        response=self.request('/api/commands','POST',command)
        self.assertEqual(response.status,200,response.body);self.assert_blind(response.body)
        result={'trade_id':response.body['trade_id']}
        self.fill(result,1)
        self.lifecycle.observe_spx(self.service_token,result['trade_id'],'private-peak',SPXObservation(6050.,FeedStatus.LIVE,self.wall(),self.mono()))
        self.assertEqual(Decimal(self.engine(result).portfolio['peak']),6050)
        status=self.request('/api/status');self.assertEqual(status.status,200);self.assert_blind(status.body)
        duplicate=self.request('/api/commands','POST',command);self.assertEqual(duplicate.status,200);self.assert_blind(duplicate.body)
        self.assertEqual(duplicate.body['result'],'DUPLICATE')
        self.assertTrue(status.body['blind_execution'])

    def test_pending_history_details_withhold_every_monetary_result_and_chart(self):
        result=self.enter();self.fill(result,1)
        detail=self.request('/api/history/'+result['trade_id'])
        self.assertEqual(detail.status,200,detail.body)
        self.assertEqual(detail.body['status'],'PENDING_RECONCILIATION')
        self.assertIsNone(detail.body['totals']);self.assertIsNone(detail.body['exit_reason'])
        for line in detail.body['per_strike']:
            self.assertNotIn('weighted_average_entry',line);self.assertNotIn('net_realized_pnl',line)
        chart=self.request('/api/history/'+result['trade_id']+'/chart')
        self.assertEqual(chart.body['status'],'WITHHELD_BLIND_EXECUTION')
        self.assertNotIn('execution_markers',chart.body)
        summary=self.request('/api/history/summary')
        self.assertEqual(summary.body['total_completed_trades'],0)
        self.assertIsNone(summary.body['average_net_realized_pnl'])

    def test_nested_unknown_performance_fields_fail_closed_under_allowlists(self):
        raw={'lifecycle':{'trades':[{'trade_id':'test','allocation':[{'strike':6000,'target':1,'owned':1,'pnl':'999'}],
            'premium_protection':[{'quantity':1,'weighted_average_premium':'10','peak':'6020','unrealized':'999'}],
            'favorable_spx_peak':'6050','new_unrealized_field':'999'}]},'new_unknown_performance':'999'}
        public=dashboard_response('/api/status',raw)
        self.assert_blind(public);self.assertNotIn('999',json.dumps(public))

    def test_completed_realized_results_only_after_verified_finalization(self):
        result=self.complete();response=self.request('/api/history/'+result['trade_id'])
        self.assertEqual(response.status,200,response.body)
        self.assertEqual(response.body['status'],'FINALIZED')
        self.assertIsNotNone(response.body['totals']['net_realized_pnl'])
        self.assertEqual(response.body['data_origin'],'OFFLINE_FIXTURE');self.assertTrue(response.body['synthetic'])
        self.assertEqual(self.request('/api/history').body['total_records'],1)

    def test_pagination_direction_search_and_date_filters(self):
        first=self.complete('CALL');second=self.complete('PUT')
        all_rows=self.request('/api/history?page_size=1&page=1').body
        self.assertEqual(all_rows['total_records'],2);self.assertEqual(len(all_rows['items']),1)
        self.assertEqual(all_rows['items'][0]['trade_id'],second['trade_id'])
        next_page=self.request('/api/history?page_size=1&page=2').body
        self.assertEqual(next_page['items'][0]['trade_id'],first['trade_id'])
        self.assertEqual(self.request('/api/history?page_size=1&page=3').body['items'],[])
        self.assertEqual(self.request('/api/history?direction=CALL').body['total_records'],1)
        self.assertEqual(self.request('/api/history?'+urlencode({'search':first['trade_id']})).body['total_records'],1)
        self.assertEqual(self.request('/api/history?start_date=2026-10-23&end_date=2026-10-23').body['total_records'],2)
        self.assertEqual(self.request('/api/history?end_date=2026-10-22').body['total_records'],0)
        self.assertEqual(self.request('/api/history/summary?direction=PUT').body['total_completed_trades'],1)

    def test_pending_query_and_empty_aggregate_are_explicit(self):
        result=self.enter();self.fill(result,1)
        response=self.request('/api/history?state=PENDING_RECONCILIATION')
        self.assertEqual(response.body['total_records'],1)
        self.assertIsNone(response.body['items'][0]['totals'])
        self.assertEqual(self.request('/api/history').body['total_records'],0)
        empty=self.request('/api/history/summary').body
        self.assertEqual(empty['total_net_realized_pnl'],'0');self.assertIsNone(empty['win_rate_percent'])

    def test_unauthenticated_expired_revoked_and_unknown_sessions_get_no_history(self):
        result=self.complete()
        for path in ('/api/history','/api/history/summary','/api/history/'+result['trade_id'],'/api/history/'+result['trade_id']+'/chart'):
            response=self.request(path,token='x'*43)
            self.assertEqual(response.status,401,response.body)
            self.assertNotIn('items',response.body);self.assertNotIn('totals',response.body)
        self.security.logout(self.session.token,self.session.csrf_token)
        self.assertEqual(self.request('/api/history').status,401)

    def test_valid_other_user_cannot_access_account_trade_or_performance(self):
        result=self.complete()
        other=MortificatioSecurity(self.root/'other-owner.sqlite',self.security.policy,self.security.application_policy,lambda:self.now)
        key=OfflineAuthenticator();bootstrap=other.provision_owner_bootstrap();options=other.begin_registration(bootstrap,'127.0.0.1')
        other.complete_registration(options['ceremony_token'],key.registration(options['public_key']),'127.0.0.1')
        # Install an independently verified test principal, then use REAL WebAuthn
        # assertion verification on the main store. No product bypass is added.
        with other.store.database() as db:
            user=tuple(db.execute('SELECT * FROM users').fetchone());credential=tuple(db.execute('SELECT * FROM credentials').fetchone())
        with self.security.store.database() as db:
            db.execute('INSERT INTO users VALUES (?,?,?)',user);db.execute('INSERT INTO credentials VALUES (?,?,?,?,?,?,?)',credential)
        options=self.security.begin_login('127.0.0.1')
        session=self.security.complete_login(options['ceremony_token'],key.assertion(options['public_key']),'127.0.0.1')
        self.assertEqual(self.request('/api/history',token=session.token).body['total_records'],0)
        self.assertEqual(self.request('/api/history/summary',token=session.token).body['total_completed_trades'],0)
        self.assertEqual(self.request('/api/history/'+result['trade_id'],token=session.token).status,404)
        self.assertEqual(self.request('/api/history/'+str(uuid.uuid4()),token=session.token).status,404)

    def test_history_endpoints_cannot_write_or_select_another_account_or_source(self):
        result=self.complete();before=self.engine(result).sequence
        for path in ('/api/history','/api/history/summary','/api/history/'+result['trade_id']):
            self.assertNotEqual(self.request(path,'POST').status,200)
        for query in ('account=DU9999999','source=REAL_IBKR','source=LIVE','direction=SELL','state=bogus',
                      'page=0','page=-1','page_size=101','page=1&page=2','start_date=2026-99-99','start_date=2026-10-24&end_date=2026-10-23'):
            self.assertEqual(self.request('/api/history?'+query).status,400,query)
        self.assertEqual(before,self.engine(result).sequence)

    def test_history_query_does_not_extend_or_create_a_trading_grant(self):
        result=self.complete();self.now+=181
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(self.request('/api/history').status,200)
        from mortificatio_models import TradeCommand
        with self.assertRaises(ApplicationError):self.lifecycle.direction_command(self.session.token,self.session.csrf_token,TradeCommand(str(uuid.uuid4()),'PUT',1))

    def test_emergency_response_does_not_reveal_profit_trigger_or_prices(self):
        result=self.enter();self.fill(result,1)
        response=self.request('/api/exit-all','POST',{'request_id':str(uuid.uuid4())})
        self.assertEqual(response.status,200);self.assert_blind(response.body)
        self.assertNotIn('exit_reason',response.body)

    def test_history_access_rate_limit_is_independent_of_trading_authority(self):
        for _ in range(self.security.policy.requests_per_minute):self.assertEqual(self.request('/api/history').status,200)
        self.assertEqual(self.request('/api/history').status,429)
        result=self.enter();self.fill(result,1)
        self.exit_strategy(result)
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)

    def test_expired_dashboard_cannot_retrieve_history_but_service_still_reconciles(self):
        result=self.enter();self.fill(result,1);self.now+=901
        self.assertEqual(self.request('/api/history?state=ALL').status,401)
        self.lifecycle.supervise(self.service_token)
        self.assertTrue(self.engine(result).portfolio['halted'])


if __name__=='__main__':unittest.main()
