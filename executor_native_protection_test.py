"""Synthetic native stop lifecycle, economic corrections and adversarial races."""
from dataclasses import replace
from types import MethodType
import sqlite3
import unittest
import executor_portfolio_test as f
from executor_native_protection import NativeProtectionCoordinator,order_lifecycle
from executor_paper_types import BrokerOrderRecord,PaperBrokerError,PositionRecord
from simulation_evidence import VerifiedOptionContract


def native_order(fake,intent,status='Submitted',**changes):
    item=fake.ledger.state['intents'][intent]
    if not item.get('native_managed'):return f.PortfolioFakeBroker.order(fake,intent,status,**changes)
    filled=sum(e.quantity for e in fake.active.values() if e.order_ref==item['order_ref'])
    record=BrokerOrderRecord(fake.ledger.approval.account,item['order_ref'],item['order_id'],item['order_id']+1000,
        item['client_id'],VerifiedOptionContract(**item['contract']),item['side'],item['quantity'],filled,item['quantity']-filled,status,'STP',item['stop_price'])
    record=replace(record,**changes);fake.orders[intent]=record
    fake.ledger.observe_order(fake.generation,fake.event(),record);return record


class NativeProtectionTest(unittest.TestCase):
    for _name in ('fresh','account','quotes','schedule','own','restart'):locals()[_name]=getattr(f.PortfolioTest,_name)
    def setUp(self):
        f.PortfolioTest.setUp(self);self.fake.order=MethodType(native_order,self.fake)
        self.manager=NativeProtectionCoordinator(self.ledger);self.manager.configure()
    def state(self,key):return order_lifecycle(self.ledger,self.ledger.state['intents'][key])
    def stop(self,cid=101,ack=True):
        self.assertTrue(self.fake.reconcile()['matched'])
        key=self.manager.plan(cid,'protect:'+str(self.ledger.sequence))
        self.assertTrue(self.fake.reconcile()['matched'])
        self.manager.bind_synthetic(key,max((i['order_id'] or 0 for i in self.ledger.state['intents'].values()),default=999)+1)
        if ack:self.fake.order(key);self.assertTrue(self.fake.reconcile()['matched'])
        return key
    def partial(self):
        ids=self.fake.dispatch(self.schedule());self.fake.match(capacity=1,side='BUY');self.assertTrue(self.fake.reconcile()['matched']);return ids
    def test_partial_fill_before_protection_ack_fences_all_acquisitions(self):
        self.partial();self.assertEqual(sum(self.ledger.positions().values()),5)
        self.assertTrue(self.ledger.portfolio['halted']);self.assertTrue(self.manager.status()['unresolved'])
        key=self.stop(ack=False);self.assertEqual(self.state(key)['phase'],'SUBMISSION_PENDING')
        self.assertFalse(self.manager.status()['strikes'][0]['covered'])
    def test_prepared_and_acknowledged_stop_are_not_working_coverage(self):
        self.own();key=self.stop(ack=False);self.fake.order(key,'PreSubmitted')
        self.assertTrue(self.fake.reconcile()['matched']);self.assertEqual(self.state(key)['phase'],'BROKER_ACKNOWLEDGED')
        self.assertFalse(self.manager.status()['strikes'][0]['covered'])
        self.fake.order(key);self.assertTrue(self.fake.reconcile()['matched'])
        self.assertTrue(self.manager.status()['strikes'][0]['covered']);self.assertFalse(self.manager.status()['real_broker_verified'])
    def test_rejected_protection_retains_reservations_until_terminal_proof(self):
        self.own();key=self.stop();self.fake.order(key,'Inactive')
        self.assertFalse(self.fake.reconcile()['matched']);self.assertEqual(self.state(key)['phase'],'RECONCILIATION_REQUIRED')
        self.manager.record_rejection(key,201,'fixture-rejection')
        self.assertEqual(self.state(key)['phase'],'REJECTED')
        self.assertEqual(self.ledger.reservations('SELL',101),25)
    def test_small_stop_and_late_entry_fill_restore_gap_without_double_selling(self):
        ids=self.partial();key=self.stop();self.fake.execution(ids[0],1,'10')
        self.assertTrue(self.fake.reconcile()['matched'])
        row=self.manager.status()['strikes'][0];self.assertEqual(row['owned'],2);self.assertFalse(row['covered'])
        extra=self.manager.plan(101,'extra-protection');self.assertEqual(self.ledger.state['intents'][extra]['quantity'],1)
        self.assertEqual(self.ledger.reservations('SELL',101),2)
    def test_duplicate_executions_are_not_counted_twice(self):
        ids=self.partial();r=next(iter(self.fake.executions.values()))
        self.ledger.observe_execution(self.fake.generation,'duplicate-execution',r)
        self.assertEqual(sum(self.ledger.positions().values()),5)
        self.assertEqual(len(self.ledger.portfolio['protection_obligations']),5)
    def test_missing_order_status_repaired_by_complete_snapshot(self):
        self.own();key=self.stop(ack=False)
        i=self.ledger.state['intents'][key]
        self.fake.orders[key]=BrokerOrderRecord(self.ledger.approval.account,i['order_ref'],i['order_id'],i['order_id']+1000,i['client_id'],self.contracts[0],'SELL',25,0,25,'Submitted','STP',i['stop_price'])
        self.assertTrue(self.fake.reconcile()['matched']);self.assertEqual(self.state(key)['phase'],'WORKING')
    def test_execution_before_order_status_is_economic_truth(self):
        self.own();key=self.stop(ack=False);r=self.fake.execution(key,5,'6.5',update_order=False)
        self.assertEqual(self.ledger.positions()[101],20);self.assertEqual(self.state(key)['phase'],'PARTIALLY_FILLED')
        self.assertFalse(self.fake.reconcile()['matched'])
        self.fake.order(key);self.assertTrue(self.fake.reconcile()['matched'])
    def test_late_execution_during_cancellation_keeps_remaining_owned_exact(self):
        self.own();key=self.stop();self.manager.request_replacement(key,'replace')
        self.fake.execution(key,5,'6.75');self.fake.order(key,'Cancelled');self.assertTrue(self.fake.reconcile()['matched'])
        self.assertEqual(self.ledger.positions()[101],20);self.assertEqual(self.ledger.reservations('SELL',101),0)
    def test_stop_execution_during_exit_all_cannot_double_liquidate(self):
        self.own();key=self.stop();self.ledger.trigger_exit('panic','EMERGENCY_EXIT_ALL',True)
        self.fake.execution(key,25,'6.5');self.assertTrue(self.fake.reconcile()['matched'])
        actions=self.schedule();self.assertEqual(sum(a.get('quantity',0) for a in actions if a.get('side')=='SELL'),100)
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
    def test_replacement_before_old_cancellation_is_forbidden(self):
        self.own();key=self.stop();self.manager.request_replacement(key,'replace')
        with self.assertRaises(PaperBrokerError):self.manager.plan(101,'replacement-too-soon',key)
        self.assertEqual(self.ledger.reservations('SELL',101),25)
    def test_conflicting_broker_positions_never_establish_flat(self):
        self.own();key=self.stop()
        result=self.fake.reconcile(positions=())
        self.assertFalse(result['matched']);self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
    def test_restart_preserves_obligations_and_requires_fresh_stop_proof(self):
        self.own();key=self.stop();before=self.ledger.portfolio['protection_obligations'].copy()
        self.restart();self.manager=NativeProtectionCoordinator(self.ledger)
        self.assertTrue(self.ledger.portfolio['halted']);self.assertTrue(self.manager.status()['unresolved'])
        self.assertEqual(self.ledger.portfolio['protection_obligations'],before)
        self.assertTrue(self.fake.reconcile()['matched']);self.assertTrue(self.manager.status()['strikes'][0]['covered'])
    def test_unexpected_reconnect_order_blocks_reconciliation(self):
        self.own();key=self.stop();row=self.fake.orders[key]
        self.fake.orders['unexpected']=replace(row,order_ref='UNKNOWN',order_id=9999,perm_id=19999)
        self.assertFalse(self.fake.reconcile()['matched']);self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
    def test_correction_reduces_buy_fill_and_retains_excess_stop_as_uncertain(self):
        ids=self.own();key=self.stop();original=next(e for e in self.fake.executions.values() if e.order_ref==self.ledger.state['intents'][ids[0]]['order_ref'])
        corrected=original.exec_id.rsplit('.',1)[0]+'.02'
        self.fake.execution(ids[0],20,'10',exec_id=corrected,correction_of=original.exec_id)
        self.assertEqual(self.ledger.positions()[101],20);self.assertEqual(self.ledger.reservations('SELL',101),25)
        self.assertFalse(self.fake.reconcile()['matched']);self.assertTrue(self.ledger.portfolio['native_protection']['uncertain'])
        actions=self.schedule();self.assertTrue(any(a.get('intent_id')==key and a['type']=='CANCEL_REQUIRED' for a in actions))
        self.assertEqual(self.ledger.reservations('SELL',101),25)
        self.fake.cancel_actions(actions);self.assertEqual(self.ledger.reservations('SELL',101),0)
    def test_busted_stop_fill_restores_owned_exposure_and_protection_obligation(self):
        self.own();key=self.stop();r=self.fake.execution(key,25,'6.75')
        self.assertTrue(self.fake.reconcile()['matched']);self.assertNotIn(101,self.ledger.positions())
        self.fake.execution(key,0,'0',exec_id=r.exec_id.rsplit('.',1)[0]+'.02',correction_of=r.exec_id,bust=True)
        self.assertEqual(self.ledger.positions()[101],25);self.assertTrue(self.ledger.portfolio['halted'])
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
    def test_simultaneous_strategy_and_emergency_exit_preserves_sell_bound(self):
        self.own();key=self.stop();self.ledger.trigger_exit('normal','TRAILING_STOP')
        self.ledger.trigger_exit('panic','EMERGENCY_EXIT_ALL',True)
        self.schedule();self.schedule()
        self.assertEqual(self.ledger.reservations('SELL',101),25)
        self.assertLessEqual(sum(self.ledger.reservations('SELL',c.con_id) for c in self.contracts),125)
    def test_database_failure_during_snapshot_proof_cannot_release_reservations(self):
        self.own();key=self.stop();self.fake.order(key,'Cancelled')
        self.ledger.db.execute("CREATE TEMP TRIGGER fail_native BEFORE INSERT ON events WHEN NEW.kind='RECONCILED' BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.fake.reconcile()
        self.assertEqual(self.ledger.reservations('SELL',101),25);self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
    def test_partial_stop_liquidation_retains_residual(self):
        self.own();key=self.stop();self.fake.execution(key,7,'6.75');self.assertTrue(self.fake.reconcile()['matched'])
        self.assertEqual(self.ledger.positions()[101],18);self.assertEqual(self.ledger.reservations('SELL',101),18)
    def test_zero_position_report_with_working_sell_is_not_flat(self):
        self.own();key=self.stop();s=self.fake.snapshot(positions=())
        self.assertFalse(self.ledger.reconcile(s)['matched']);self.assertEqual(self.ledger.reservations('SELL',101),25)
    def test_all_five_native_stops_during_emergency_reserve_all_owned_quantity(self):
        self.own();keys=[self.stop(c.con_id) for c in self.contracts]
        self.ledger.trigger_exit('panic','EMERGENCY_EXIT_ALL',True);actions=self.schedule()
        self.assertEqual(sum(self.ledger.reservations('SELL',c.con_id) for c in self.contracts),125)
        self.assertFalse(any(a.get('type')=='SIMULATED_LIMIT_INTENT' for a in actions))
        self.assertEqual(sum(a['type']=='CANCEL_REQUIRED' for a in actions),5)

    def test_expiration_and_rejection_reasons_require_terminal_reconciliation(self):
        self.own();key=self.stop()
        with self.assertRaises(PaperBrokerError):self.manager.annotate_terminal(key,'expired','expiry-proof')
        self.fake.order(key,'Cancelled');self.assertTrue(self.fake.reconcile()['matched'])
        self.manager.annotate_terminal(key,'expired','expiry-proof');self.assertEqual(self.state(key)['phase'],'EXPIRED')

    def test_partial_fill_without_quote_freshness_can_prepare_reduction_only_protection(self):
        self.partial();self.fake.advance(2);self.assertTrue(self.fake.reconcile()['matched'])
        key=self.manager.plan(101,'stale-data-protection')
        self.assertEqual(self.ledger.state['intents'][key]['quantity'],1)
        self.assertTrue(self.ledger.portfolio['halted'])

    def test_native_limit_dispatch_bypass_is_rejected(self):
        self.own();key=self.manager.plan(101,'stop')
        self.assertTrue(self.fake.reconcile()['matched'])
        with self.assertRaises(PaperBrokerError):self.ledger.dispatch_batch(self.fake.generation,[key],self.account())


if __name__=='__main__':unittest.main()
