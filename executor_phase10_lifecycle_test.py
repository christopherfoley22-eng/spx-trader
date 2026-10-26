"""Authenticated prepared entries and protection policy integrate before dispatch."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from unittest.mock import patch
import sqlite3
import unittest
import uuid
import executor_lifecycle_test as fixtures
from executor_entry_readiness import StreamingEntryEvidence
from mortificatio_models import ApplicationError,TradeCommand


class UniqueProvider:
    def __init__(self,provider):self.provider=provider;self.funds='200000'
    def snapshot(self,direction):
        self.provider.funds=self.funds;e=self.provider.snapshot(direction)
        if direction=='PUT':
            e=replace(e,quotes=tuple(replace(q,quote_id='P:'+q.quote_id,option=replace(q.option,
                contract=replace(q.option.contract,con_id=q.option.contract.con_id+10000),quote_con_id=q.option.quote_con_id+10000)) for q in e.quotes))
        import executor_allocation_test as allocation_fixtures
        return allocation_fixtures.sync_protection_plan(e)


class Phase10LifecycleTest(unittest.TestCase):
    for _name in ('setUp','cleanup_lifecycle','enroll','login','grant','authorize','wall','mono','verify','enter','engine','orders','fill','last','restart'):
        locals()[_name]=getattr(fixtures.LifecycleTest,_name)
    def prepare(self):
        self.provider=UniqueProvider(self.provider);self.allocation.provider=self.provider
        return self.lifecycle.prepare_entry_candidates(self.service_token)

    def test_call_and_put_one_command_uses_prepared_allocations_and_existing_reservations(self):
        self.prepare();result=self.enter('PUT')
        self.assertIn('prepared_entry',result['allocation'])
        self.assertEqual(result['allocation']['total_contracts'],125)
        self.assertEqual(sum(i['quantity'] for i in self.orders(result)),125)
        self.assertFalse(result['submission_enabled'])

    def test_double_click_and_opposite_direction_cannot_double_reserve(self):
        self.prepare();self.authorize();command=TradeCommand(str(uuid.uuid4()),'CALL',0)
        def call(_):return self.lifecycle.direction_command(self.session.token,self.session.csrf_token,command)
        with ThreadPoolExecutor(max_workers=2) as p:results=list(p.map(call,range(2)))
        self.assertEqual(sum(x['result']=='ACCEPTED_OFFLINE' for x in results),1)
        with self.assertRaises(ApplicationError):
            self.lifecycle.direction_command(self.session.token,self.session.csrf_token,TradeCommand(str(uuid.uuid4()),'PUT',0))
        self.assertEqual(len(self.orders(results[0])),5)

    def test_buying_power_change_and_expired_grant_block_cached_entry(self):
        self.prepare();self.provider.funds='500'
        with self.assertRaises(ApplicationError):self.enter()
        with self.security.store.database() as db:self.assertEqual(db.execute('SELECT count(*) FROM allocation_reservations').fetchone()[0],0)

    def test_expired_trading_grant_cannot_use_valid_cached_plan(self):
        self.prepare();self.authorize();self.now+=181
        with self.assertRaises(ApplicationError):
            self.lifecycle.direction_command(self.session.token,self.session.csrf_token,TradeCommand(str(uuid.uuid4()),'CALL',0))

    def test_partial_fill_policy_is_durable_before_first_acquisition_and_reports_critical_gap(self):
        self.prepare();self.lifecycle.configure_partial_fill_protection(self.service_token,'phase10-offline-v1')
        result=self.enter();self.assertTrue(self.engine(result).portfolio['protection_lifecycle'])
        self.fill(result,1);self.assertTrue(self.engine(result).portfolio['halted'])
        self.assertEqual(self.lifecycle.protective_health(self.service_token)['condition'],'CRITICAL_UNRESOLVED_PROTECTION')
        self.restart();self.assertTrue(self.engine(result).portfolio['protection_lifecycle'])
        self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])

    def test_preparation_and_policy_require_independent_local_authority(self):
        self.authorize()
        with self.assertRaises(ApplicationError):self.lifecycle.prepare_entry_candidates(self.session.token)
        with self.assertRaises(ApplicationError):self.lifecycle.configure_partial_fill_protection(self.session.token,'x')

    def test_background_worker_is_event_driven_and_stopping_invalidates_candidates(self):
        provider=StreamingEntryEvidence(self.mono);done=Event();original=self.lifecycle.prepare_entry_candidates
        def observed(*args,**kwargs):
            out=original(*args,**kwargs);done.set();return out
        with patch.object(self.lifecycle,'prepare_entry_candidates',side_effect=observed):
            worker=self.lifecycle.start_entry_readiness(self.service_token,provider)
            provider.publish('CALL',UniqueProvider(self.provider).snapshot('CALL'))
            self.assertTrue(done.wait(3))
        self.assertIn('CALL',self.allocation.readiness.candidates)
        worker.close();self.assertFalse(self.allocation.readiness.candidates)

    def test_fill_journal_failure_replays_protection_fence_on_restart(self):
        self.prepare();self.lifecycle.configure_partial_fill_protection(self.service_token,'atomic-fill-protection')
        result=self.enter();engine=self.engine(result)
        engine.db.execute("CREATE TEMP TRIGGER fail_fill BEFORE INSERT ON events WHEN NEW.kind='PORT_EXECUTION' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.fill(result,1)
        self.assertFalse(engine.positions())
        self.assertTrue(self.lifecycle.checkpoint.read()['fenced'])
        self.restart();self.lifecycle.supervise(self.service_token)
        self.assertEqual(sum(self.engine(result).positions().values()),5)
        self.assertTrue(self.engine(result).portfolio['halted'])
        self.assertEqual(len(self.engine(result).portfolio['protection_lifecycle']['recovery']),5)

    def shift_after_admission(self,shift):
        original=self.provider.snapshot
        def shifted(direction):
            e=original(direction)
            return replace(e,quotes=tuple(replace(q,quote_id='shift:'+q.quote_id,
                option=replace(q.option,bid=q.option.bid+shift,ask=q.option.ask+shift)) for q in e.quotes))
        def hook(stage,trade):
            if stage=='AFTER_CONFIGURATION':self.provider.snapshot=shifted
        self.lifecycle.fault_hook=hook

    def test_market_moves_beyond_price_cap_after_reservation_never_dispatches_old_quote(self):
        self.prepare();self.shift_after_admission(1.)
        result=self.enter();self.assertFalse(self.orders(result))
        self.assertTrue(self.engine(result).portfolio['halted'])
        self.assertEqual(self.engine(result).portfolio['quotes']['105']['option']['ask'],11.)

    def test_fresh_price_within_reserved_limit_updates_dispatch_without_resizing(self):
        self.prepare();self.shift_after_admission(.15)
        result=self.enter();orders=self.orders(result)
        self.assertEqual(sum(i['quantity'] for i in orders),125)
        self.assertTrue(all(float(i['limit_price'])==10.15 for i in orders))


if __name__=='__main__':unittest.main()
