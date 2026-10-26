"""Authenticated allocation/protection/history integration, all synthetic."""
from dataclasses import replace
from decimal import Decimal
import sqlite3,unittest,uuid
from unittest.mock import patch
import executor_trade_history_test as f
from executor_native_protection import NativeProtectionCoordinator
from mortificatio_models import ApplicationError,TradeCommand
from executor_blind_presentation import dashboard_response


class NativeLifecycleTest(unittest.TestCase):
    for _name in ('setUp','cleanup_lifecycle','enroll','login','grant','authorize','wall','mono','verify','enter','engine','orders','fill','last','restart','one_strike','detail'):
        locals()[_name]=getattr(f.HistoryTest,_name)
    def protect(self,r):
        cid=next(iter(self.engine(r).positions()))
        key=self.lifecycle.prepare_native_protection(self.service_token,r['trade_id'],cid,str(uuid.uuid4()))
        self.lifecycle.bind_native_simulation(self.service_token,r['trade_id'],key)
        return key
    def test_feasibility_flag_without_exact_protection_plan_cannot_reserve_entry(self):
        original=self.provider.snapshot
        self.provider.snapshot=lambda d:replace(original(d),capacity_context=replace(original(d).capacity_context,protection_plan=None))
        with self.assertRaises(ApplicationError):self.enter()
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT count(*) FROM allocation_reservations').fetchone()[0],0)
    def test_every_entry_fill_is_fenced_even_without_old_optional_protection_flag(self):
        self.one_strike();r=self.enter();self.fill(r,1)
        self.assertTrue(self.engine(r).portfolio['halted'])
        self.assertEqual(self.lifecycle.protective_health(self.service_token)['condition'],'CRITICAL_UNRESOLVED_PROTECTION')
        with self.assertRaises(ApplicationError):self.enter('PUT')
        self.assertEqual(sum(self.engine(r).positions().values()),1)
    def test_native_stop_actual_gap_fill_accounting_is_not_trigger_price_estimate(self):
        self.one_strike();r=self.enter();self.fill(r,1);key=self.protect(r);e=self.engine(r)
        self.fixture.fill(e.state['intents'][key]['order_ref'],1,'6.50',self.wall(),'native-exit.01')
        self.lifecycle.supervise(self.service_token)
        d=self.detail(r);self.assertEqual(d['status'],'FINALIZED',d)
        self.assertEqual(Decimal(d['totals']['net_realized_pnl']),Decimal('-351.30'))
    def test_bust_restores_exposure_revokes_finalization_and_reholds_reservation(self):
        self.one_strike();r=self.enter();self.fill(r,1);key=self.protect(r);e=self.engine(r);ref=e.state['intents'][key]['order_ref']
        self.fixture.fill(ref,1,'6.5',self.wall(),'restored.01');self.lifecycle.supervise(self.service_token)
        self.assertEqual(self.detail(r)['status'],'FINALIZED')
        self.fixture.fill(ref,0,'0',self.wall(),'restored.02',fee='0',correction_of='restored.01',bust=True)
        self.lifecycle.supervise(self.service_token)
        d=self.detail(r);self.assertEqual(d['status'],'PENDING_RECONCILIATION');self.assertIsNone(d['totals'])
        self.assertTrue(d['prior_finalization_revoked']);self.assertEqual(sum(e.positions().values()),1)
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT state FROM allocation_reservations WHERE trade_id=?',(r['trade_id'],)).fetchone()[0],'HELD')
        # Fill only the newly authorized working LMT, not the canceled historical
        # stop. Injecting fills into both would be contradictory broker evidence.
        closing=next(i for i in self.orders(r,'SELL') if not i.get('native_managed') and not i['terminal_proven'])
        self.fixture.fill(closing['order_ref'],1,'9.9',self.wall(),'corrected-close.01')
        self.lifecycle.supervise(self.service_token);d=self.detail(r)
        self.assertEqual(d['status'],'FINALIZED',d);self.assertEqual(Decimal(d['totals']['net_realized_pnl']),Decimal('-11.30'))
    def test_dashboard_grant_expiration_does_not_revoke_reduction_only_native_service(self):
        self.one_strike();r=self.enter();self.fill(r,1);self.now+=181
        self.lifecycle.supervise(self.service_token)
        key=self.protect(r);e=self.engine(r)
        self.assertEqual(e.state['intents'][key]['side'],'SELL')
        self.assertFalse(self.last(r)['reconciled_flat'])
        with self.assertRaises(ApplicationError):self.enter()
    def test_unauthed_request_cannot_read_native_recovery_state(self):
        self.one_strike();r=self.enter();self.fill(r,1)
        with self.assertRaises(ApplicationError) as c:
            self.lifecycle.direction_command('unknown','unknown',TradeCommand(str(uuid.uuid4()),'CALL',0))
        self.assertNotEqual(c.exception.code,'NATIVE_PROTECTION_RECOVERY_FENCES_ENTRY')
    def test_blind_projection_does_not_reveal_native_prices_or_protection_plan(self):
        self.one_strike();r=self.enter();self.fill(r,1);self.protect(r)
        view=dashboard_response('/api/commands',dict(r,lifecycle=self.last(r)))
        import json
        text=json.dumps(view)
        for field in ('required_stop_price','protection_plan','contracts_json','weighted_average','modeled_downside','native_protection_state'):
            self.assertNotIn(field,text)
        self.assertEqual(view['lifecycle']['risk_control_health'],'HEALTHY')
    def test_foundation_outage_retains_native_obligations_and_independent_planning(self):
        self.one_strike();r=self.enter();self.fill(r,1);e=self.engine(r)
        with patch.object(self.security.store,'database',side_effect=sqlite3.OperationalError('foundation down')):
            key=self.lifecycle.prepare_native_protection(self.service_token,r['trade_id'],next(iter(e.positions())),'independent-native')
            self.assertEqual(e.state['intents'][key]['quantity'],1)
            self.assertFalse(e.state['intents'][key]['terminal_proven'])
        self.assertEqual(len(e.portfolio['protection_obligations']),1)


if __name__=='__main__':unittest.main()
