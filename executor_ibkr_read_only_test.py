"""Stage 5A fake SDK callbacks and transport; never opens any socket."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

from executor_broker_snapshot import ReadOnlySnapshotMachine
from executor_dry_run import DryRunExecutor
from executor_ibkr_observation import SIZING_TAGS
from executor_ibkr_read_only import (ReadOnlyPaperValidation, SettingsVerification,
    SUMMARY, SPX_DETAILS, CHAIN, SPX_QUOTE)
from executor_paper_types import ApprovedPaperAccount, PaperBrokerError
from simulation_evidence import EvidenceError

transport = importlib.import_module('ibkr_paper_read_only_validation')
NOW = datetime(2026, 10, 23, 14, 30, tzinfo=timezone.utc)
ACCOUNT = 'DU1234567'


def config():
    return (ApprovedPaperAccount(ACCOUNT, 'GUI-PAPER-ACCOUNT-VERIFIED', NOW+timedelta(minutes=5)),
            SettingsVerification('GUI-READ-ONLY-VERIFIED', NOW, NOW+timedelta(minutes=5), True, True))


def spx():
    return Obj(symbol='SPX', secType='IND', currency='USD', conId=416904, exchange='CBOE')


class FakeWire(transport.PaperReadOnlyProbe):
    """Exercises the actual transport facade and SDK-shaped callbacks, no socket."""
    instances = []

    def __init__(self, validation):
        super().__init__(validation)
        self.calls = []
        self.instances.append(self)

    def connect(self, host, port, clientId, *, authorized=False):
        with patch.object(transport.ReadOnlyProbe, 'connect', return_value=None):
            super().connect(host, port, clientId, authorized=authorized)
        self.nextValidId(7)
        self.managedAccounts(ACCOUNT+',')

    def run(self):
        pass

    def reqAccountSummary(self, reqId, group, tags):
        self.calls.append(('reqAccountSummary', reqId))
        for tag in tags.split(','):
            self.accountSummary(reqId, ACCOUNT, tag, '100000', 'USD')
        self.accountSummaryEnd(reqId)

    def reqPositions(self):
        self.calls.append(('reqPositions',))
        self.positionEnd()

    def reqAllOpenOrders(self):
        self.calls.append(('reqAllOpenOrders',))
        self.openOrderEnd()

    def reqContractDetails(self, reqId, contract):
        self.calls.append(('reqContractDetails', reqId))
        if reqId == SPX_DETAILS:
            c = spx()
        else:
            c = Obj(**vars(contract))
            c.conId = reqId+10000
        self.contractDetails(reqId, Obj(contract=c))
        self.contractDetailsEnd(reqId)

    def reqSecDefOptParams(self, reqId, symbol, exchange, secType, conId):
        self.calls.append(('reqSecDefOptParams', reqId))
        self.securityDefinitionOptionParameter(reqId, 'SMART', conId, 'SPXW', '100', {'20261023'}, {5995., 6000., 6005.})
        self.securityDefinitionOptionParameterEnd(reqId)

    def reqMarketDataType(self, code):
        self.calls.append(('reqMarketDataType', code))

    def reqMktData(self, reqId, contract, ticks, snapshot, regulatory, options):
        self.calls.append(('reqMktData', reqId))
        assert not snapshot and not regulatory and ticks == ''
        self.marketDataType(reqId, 1)
        if reqId == SPX_QUOTE:
            self.tickPrice(reqId, 4, 6000., None)
        else:
            self.tickPrice(SPX_QUOTE, 4, 6000., None)
            self.tickPrice(reqId, 1, 9.9, None)
            self.tickPrice(reqId, 2, 10., None)
            self.tickOptionComputation(reqId, 13, 0, .25, .48 if contract.right == 'C' else -.48,
                                       10., 0., .01, .2, -.5, 6000.)
            self.tickString(reqId, 45, str(int(NOW.timestamp())))


class ReadOnlyValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now, self.mono = NOW, 100.
        self.approval, self.settings = config()
        self.validation = self.make()
        self.app = transport.PaperReadOnlyProbe(self.validation)

    def make(self, **changes):
        return ReadOnlyPaperValidation(changes.pop('approval', self.approval), changes.pop('settings', self.settings),
                                       lambda: self.now, lambda: self.mono, **changes)

    def identity(self, app=None):
        app = app or self.app
        app.managedAccounts(ACCOUNT+',')
        app.nextValidId(7)
        self.assertTrue(app.validation.verified)

    def exact_streams(self):
        self.identity()
        self.app.contractDetails(SPX_DETAILS, Obj(contract=spx()))
        self.app.contractDetailsEnd(SPX_DETAILS)
        self.app.securityDefinitionOptionParameter(CHAIN, 'SMART', spx().conId, 'SPXW', '100', {'20261023'}, {5995., 6000., 6005.})
        self.app.securityDefinitionOptionParameterEnd(CHAIN)
        self.app.marketDataType(SPX_QUOTE, 1)
        self.app.tickPrice(SPX_QUOTE, 4, 6000., None)
        pending = self.validation.plan_options()
        for req_id, spec in pending.items():
            c = transport.contract(symbol='SPX', secType='OPT', exchange='SMART', currency='USD',
                conId=req_id+10000, right=spec['right'], strike=spec['strike'], multiplier='100',
                tradingClass='SPXW', lastTradeDateOrContractMonth=spec['expiration'])
            self.app.contractDetails(req_id, Obj(contract=c))
            self.app.contractDetailsEnd(req_id)
        self.assertEqual(len(self.validation.bridges), 2)
        return pending

    def live(self):
        pending = self.exact_streams()
        self.app.tickPrice(SPX_QUOTE, 4, 6000., None)
        for key, spec in pending.items():
            req_id = key+1000
            self.app.marketDataType(req_id, 1)
            self.app.tickPrice(req_id, 1, 9.9, None)
            self.app.tickPrice(req_id, 2, 10., None)
            self.app.tickOptionComputation(req_id, 13, 0, .25, .48 if spec['right'] == 'C' else -.48,
                10., 0., .01, .2, -.5, 6000.)
        return self.validation.report()

    def test_explicit_fresh_paper_and_read_only_settings_required(self):
        for change in ({'account': 'U1234567'}, {'account': 'DUunknown'}, {'host': 'localhost'},
                       {'port': 7496}, {'port': 4001}, {'approved_until': NOW}):
            with self.subTest(change=change), self.assertRaises(PaperBrokerError):
                self.make(approval=replace(self.approval, **change))
        for change in ({'read_only_api_checked': False}, {'paper_login_verified': False},
                       {'read_only_api_checked': 1}, {'verified_at': NOW+timedelta(seconds=1)},
                       {'expires_at': NOW}, {'expires_at': NOW+timedelta(minutes=11)}):
            with self.subTest(change=change), self.assertRaises(PaperBrokerError):
                self.make(settings=replace(self.settings, **change))

    def test_no_socket_without_explicit_one_shot_authorization(self):
        with patch.object(transport.ReadOnlyProbe, 'connect') as connect:
            for args in (('127.0.0.1',7497,3,False), ('127.0.0.1',7496,3,True), ('127.0.0.1',7497,0,True)):
                with self.assertRaises(EvidenceError):
                    self.app.connect(*args[:3], authorized=args[3])
            connect.assert_not_called()
            self.app.connect('127.0.0.1',7497,3,authorized=True)
            connect.assert_called_once()
            with self.assertRaises(EvidenceError):
                self.app.connect('127.0.0.1',7497,3,authorized=True)
        with self.assertRaises(EvidenceError):
            transport.run_validation(self.make())

    def test_live_unknown_duplicate_and_ambiguous_accounts_disconnect_before_requests(self):
        for accounts in ('U1234567', 'DU9999999', ACCOUNT+',U1234567', ACCOUNT+','+ACCOUNT, '', ACCOUNT+',,'):
            app = transport.PaperReadOnlyProbe(self.make())
            app.managedAccounts(accounts)
            app.nextValidId(7)
            self.assertFalse(app.validation.verified)
            self.assertTrue(app.validation.closed)

    def test_summary_reuses_funds_validation_and_keeps_identity_redacted(self):
        self.identity()
        self.validation.begin_summary()
        for tag in SIZING_TAGS:
            self.app.accountSummary(SUMMARY, ACCOUNT, tag, '100000', 'USD')
        self.app.accountSummaryEnd(SUMMARY)
        self.app.positionEnd()
        self.app.openOrderEnd()
        report = self.validation.report()
        self.assertTrue(report['account_funds_fresh'])
        self.assertTrue(report['funds_fresh_at_summary_completion'])
        self.assertNotIn(ACCOUNT, json.dumps(report))
        self.mono += 1.01
        self.now += timedelta(seconds=1.01)
        self.assertFalse(self.validation.report()['account_funds_fresh'])
        self.assertTrue(self.validation.report()['funds_fresh_at_summary_completion'])

    def test_wrong_account_currency_missing_and_invalid_funds_fail_closed(self):
        for account, currency, value in ((ACCOUNT,'EUR','1'), ('DU9999999','USD','1'), (ACCOUNT,'USD','NaN')):
            app = transport.PaperReadOnlyProbe(self.make())
            self.identity(app)
            app.validation.begin_summary()
            app.accountSummary(SUMMARY, account, 'AvailableFunds', value, currency)
            self.assertTrue(app.validation.failure)
        self.identity()
        self.validation.begin_summary()
        self.app.accountSummaryEnd(SUMMARY)
        self.assertTrue(self.validation.failure)

    def test_exact_call_put_greeks_are_diagnostic_never_source_proof(self):
        report = self.live()
        self.assertEqual({c['right'] for c in report['exact_contracts']}, {'C','P'})
        self.assertEqual(len(report['exact_contracts']), 6)
        self.assertIsNone(report['measured_source_skew_seconds'])
        self.assertEqual(report['required_source_skew_seconds'], .25)
        self.assertFalse(report['executable'])
        for rows in report['streams'].values():
            for row in rows:
                self.assertFalse(row['source_proven'])
                for value in row['values'].values():
                    self.assertIsNone(value['source_time'])
                    self.assertIsNone(value['exchange_time'])
                    self.assertEqual(value['monotonic_receipt'], 100.)
        for ranking in report['rankings'].values():
            self.assertIsNone(ranking['selected'])

    def test_trade_timestamp_cannot_stamp_quotes_or_greeks(self):
        self.live()
        self.app.tickString(1100,45,str(int(NOW.timestamp())))
        report = self.validation.report()
        self.assertEqual(report['trade_timestamps'][0]['trade_source_time'], NOW.isoformat())
        self.assertFalse(report['trade_timestamps'][0]['proves_quote_source_time'])
        self.assertIsNone(report['measured_source_skew_seconds'])
        self.assertIsNone(report['streams']['CALL'][1]['values']['bid']['source_time'])

    def test_wrong_expiry_right_strike_or_ambiguous_details_disconnect(self):
        for change in ({'lastTradeDateOrContractMonth':'20261022'}, {'right':'P'}, {'strike':6010.},
                       {'tradingClass':'SPX'}, {'multiplier':'10'}, {'exchange':'INVALID'}):
            self.validation = self.make()
            self.app = transport.PaperReadOnlyProbe(self.validation)
            self.identity()
            self.app.contractDetails(SPX_DETAILS,Obj(contract=spx()))
            self.app.contractDetailsEnd(SPX_DETAILS)
            self.app.securityDefinitionOptionParameter(CHAIN,'SMART',spx().conId,'SPXW','100',{'20261023'},{6000.})
            self.app.securityDefinitionOptionParameterEnd(CHAIN)
            self.app.marketDataType(SPX_QUOTE,1)
            self.app.tickPrice(SPX_QUOTE,4,6000.,None)
            self.validation.plan_options()
            c = transport.contract(symbol='SPX',secType='OPT',currency='USD',right='C',strike=6000.,
                conId=123,exchange='SMART',lastTradeDateOrContractMonth='20261023',tradingClass='SPXW',multiplier='100')
            for key,value in change.items():
                setattr(c,key,value)
            self.app.contractDetails(100,Obj(contract=c))
            self.assertTrue(self.validation.closed, change)
            self.assertIsNotNone(self.validation.report()['session_failure'])

    def test_frozen_delayed_crossed_missing_greeks_stale_and_reconnect(self):
        self.live()
        self.app.marketDataType(1100,2)
        report = self.validation.report()
        self.assertIn('NONLIVE_MARKET_DATA', report['streams']['CALL'][1]['block_reasons'])
        self.app.marketDataType(1100,1)
        self.app.tickPrice(1100,1,11.,None)
        self.app.tickPrice(1100,2,10.,None)
        reasons = self.validation.report()['streams']['CALL'][1]['block_reasons']
        self.assertIn('CROSSED_QUOTE',reasons)
        self.assertIn('MISSING_GREEKS',reasons)
        self.now += timedelta(seconds=2)
        self.mono += 2
        self.assertIn('STALE_OR_FUTURE_RECEIPT',self.validation.report()['streams']['PUT'][1]['block_reasons'])
        self.app.error(-1,1100,'sensitive account '+ACCOUNT)
        self.app.error(-1,1102,'restored')
        self.assertFalse(self.validation.verified)
        self.app.nextValidId(9)
        self.assertFalse(self.validation.verified)
        self.assertNotIn(ACCOUNT,json.dumps(self.validation.report()))

    def test_entitlement_error_blocks_and_does_not_leak_broker_text(self):
        self.identity()
        self.app.error(SPX_QUOTE,354,'account '+ACCOUNT+' missing permission')
        report=self.validation.report()
        self.assertEqual(report['broker_errors'][0]['code'],354)
        self.assertFalse(report['paper_account_matched'])
        self.assertNotIn(ACCOUNT,json.dumps(report))

    def test_existing_durable_snapshot_machine_receives_real_callback_shapes(self):
        local=self.root/'local.sqlite'
        controller=DryRunExecutor(local,ACCOUNT,lambda _:True,initial_trading_day='2026-10-23')
        controller.close()
        snapshot=ReadOnlySnapshotMachine(self.root/'snapshot.json',local,ACCOUNT)
        self.addCleanup(snapshot.close)
        validation=self.make(snapshot_machine=snapshot)
        app=transport.PaperReadOnlyProbe(validation)
        self.identity(app)
        validation.begin_summary()
        for tag in SIZING_TAGS:
            app.accountSummary(SUMMARY,ACCOUNT,tag,'100000','USD')
        app.accountSummaryEnd(SUMMARY)
        app.positionEnd()
        app.openOrderEnd()
        report=validation.report()
        self.assertEqual(report['snapshot']['reconciliation'],'MATCHED_FLAT')
        self.assertFalse(report['executable'])
        self.assertIn('FULL_PAPER_LEDGER_RECONCILIATION_NOT_VALIDATED',report['block_reasons'])

    def test_full_fake_transport_flow_uses_only_allowed_requests(self):
        with patch('socket.socket', side_effect=AssertionError('OFFLINE TEST OPENED SOCKET')):
            report=transport.run_validation(self.validation,authorized=True,seconds=1,probe_factory=FakeWire)
        self.assertTrue(report['paper_account_matched'],report)
        self.assertEqual(len(report['exact_contracts']),6)
        self.assertIsNone(report['session_failure'])
        self.assertTrue(self.validation.closed)
        self.assertEqual({c[0] for c in FakeWire.instances[-1].calls},
            {'reqAccountSummary','reqPositions','reqAllOpenOrders','reqContractDetails','reqSecDefOptParams','reqMarketDataType','reqMktData'})
        self.assertTrue(all(r['selected'] is None for r in report['rankings'].values()))

    def test_socket_handshake_timeout_cannot_start_data_requests(self):
        released=threading.Event()
        class StalledWire(FakeWire):
            def connect(inner,*args,**kwargs):
                released.wait(1)
            def disconnect(inner):
                released.set()
                super().disconnect()
        with patch('socket.socket',side_effect=AssertionError('SOCKET')):
            report=transport.run_validation(self.validation,authorized=True,seconds=1,
                                            timeout=.01,probe_factory=StalledWire)
        self.assertEqual(report['session_failure'],'SOCKET_HANDSHAKE_TIMEOUT')
        self.assertFalse(report['paper_account_matched'])
        self.assertEqual(FakeWire.instances[-1].calls,[])

    def test_import_and_default_cli_preflight_never_connect(self):
        path=self.root/'private.json'
        path.write_text(json.dumps(dict(account=ACCOUNT,verification_ref='paper-gui',approved_until=self.approval.approved_until.isoformat(),
            host='127.0.0.1',port=7497,settings=dict(reference='read-only-gui',verified_at=NOW.isoformat(),
            expires_at=self.settings.expires_at.isoformat(),paper_login_verified=True,read_only_api_checked=True))))
        path.chmod(0o600)
        with patch('socket.socket',side_effect=AssertionError('SOCKET')), patch.object(transport,'datetime') as clock, patch('sys.stdout',new_callable=io.StringIO) as output:
            # Constructor uses reducer datetime; supply a fresh verification relative to its real clock instead.
            real=datetime.now(timezone.utc)
            data=json.loads(path.read_text())
            data['approved_until']=(real+timedelta(minutes=5)).isoformat()
            data['settings']['verified_at']=real.isoformat()
            data['settings']['expires_at']=(real+timedelta(minutes=5)).isoformat()
            path.write_text(json.dumps(data))
            clock.fromisoformat.side_effect=datetime.fromisoformat
            self.assertEqual(transport.main(['--config',str(path)]),0)
            self.assertFalse(json.loads(output.getvalue())['connection_attempted'])
        path.chmod(0o644)
        with self.assertRaises(EvidenceError):
            transport.load_config(path)

    def test_invalid_spx_clock_and_delayed_bootstrap_never_plan_contracts(self):
        self.identity()
        self.app.marketDataType(SPX_QUOTE,1)
        self.app.tickPrice(SPX_QUOTE,4,6000.,None)
        self.mono = 99.
        self.app.tickPrice(SPX_QUOTE,4,6001.,None)
        self.assertTrue(self.validation.closed)
        for code in (True, 0, 5):
            validation=self.make()
            app=transport.PaperReadOnlyProbe(validation)
            self.mono=100.
            self.identity(app)
            app.marketDataType(SPX_QUOTE,code)
            self.assertTrue(validation.closed)
        validation=self.make()
        app=transport.PaperReadOnlyProbe(validation)
        self.identity(app)
        app.marketDataType(SPX_QUOTE,1)
        app.tickPrice(SPX_QUOTE,4,6000.,None)
        app.tickPrice(SPX_QUOTE,68,6000.,None)
        self.assertTrue(validation.closed)

    def test_invalid_and_wrong_sign_greeks_disconnect_without_readiness(self):
        for delta in (float('nan'), -.48, 1e308):
            self.validation=self.make()
            self.app=transport.PaperReadOnlyProbe(self.validation)
            self.exact_streams()
            self.app.marketDataType(1100,1)
            self.app.tickOptionComputation(1100,13,0,.25,delta,10.,0.,.01,.2,-.5,6000.)
            self.assertTrue(self.validation.closed)
            self.assertFalse(self.validation.report()['executable'])

    def test_duplicate_contract_and_unknown_request_cannot_complete_session(self):
        self.live()
        self.app.contractDetailsEnd(100)
        self.assertTrue(self.validation.closed)
        validation=self.make()
        app=transport.PaperReadOnlyProbe(validation)
        self.identity(app)
        app.tickPrice(999999,1,10.,None)
        self.assertTrue(validation.closed)

    def test_inherited_broker_mutations_remain_hard_disabled(self):
        for method in ('placeOrder','cancelOrder','reqGlobalCancel','exerciseOptions','replaceFA','reqAutoOpenOrders','setServerLogLevel'):
            with self.assertRaises(RuntimeError):
                getattr(self.app,method)()

    def test_expired_settings_mid_session_fail_closed(self):
        self.identity()
        self.now += timedelta(minutes=6)
        self.assertFalse(self.validation.report()['paper_account_matched'])
        with self.assertRaises(PaperBrokerError):
            self.validation.plan_options()


if __name__=='__main__':
    unittest.main()
