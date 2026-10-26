"""Independent fake broker/books, five-strike limits and adverse offline schedules."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import json
import random
import subprocess
import sys
import tempfile
import unittest

from executor_market_ingestion import OptionGreeks
from executor_paper_broker import PaperBrokerLedger
from executor_paper_broker_test import FakeBroker, approval, CONTRACT, NOW, ACCOUNT
from executor_paper_types import PaperBrokerError
from executor_portfolio import OfflinePortfolioLedger, PortfolioOrderRecord
from executor_portfolio_liquidity import ExecutionPolicy, LiquidityFixture
from simulation_evidence import FeedStatus, SPXObservation, OptionObservation, VerifiedAccountSnapshot


def policy(**changes):
    return replace(ExecutionPolicy('.50','.20','.30','.50',1,25,1,2,'.05',2,3,30,20,2,200000,50000,50000000),**changes)


class PortfolioFakeBroker(FakeBroker):
    def order(self,intent,status='Submitted',**changes):
        item=self.ledger.state['intents'][intent]
        filled=sum(e.quantity for e in self.active.values() if e.order_ref==item['order_ref'])
        record=PortfolioOrderRecord(ACCOUNT,item['order_ref'],item['order_id'],item['order_id']+1000,
            item['client_id'],replace(CONTRACT,**{k:v for k,v in item['contract'].items() if k in CONTRACT.__dict__}),
            item['side'],item['quantity'],filled,item['quantity']-filled,status,'LMT',None,item['limit_price'])
        record=replace(record,**changes)
        self.orders[intent]=record
        self.ledger.observe_order(self.generation,self.event(),record)
        return record

    def dispatch(self,actions,ack=True):
        ids=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']
        if not ids:return []
        assert self.reconcile()['matched']
        self.ledger.dispatch_batch(self.generation,ids,VerifiedAccountSnapshot(ACCOUNT,ACCOUNT,True,'USD','200000','200000','200000','200000','200000',self.mono))
        if ack:
            for key in ids:self.order(key)
            assert self.reconcile()['matched']
        return ids

    def match(self,capacity=1000,side=None):
        """Actual fake liquidity is independent of displayed/estimated liquidity."""
        remaining={cid:capacity for cid in [101,102,103,104,105]}
        for key,record in list(self.orders.items()):
            if record.status not in {'Submitted','PreSubmitted','PendingCancel'} or side and record.side!=side:continue
            item=self.ledger.state['intents'][key];cid=record.contract.con_id
            outstanding=record.quantity-sum(e.quantity for e in self.active.values() if e.order_ref==record.order_ref)
            price=Decimal('10') if record.side=='BUY' else Decimal('9.9')
            if record.side=='BUY' and Decimal(item['limit_price'])<price or record.side=='SELL' and Decimal(item['limit_price'])>price:continue
            qty=min(outstanding,remaining[cid])
            if qty:
                self.execution(key,qty,str(price));remaining[cid]-=qty

    def cancel_actions(self,actions):
        for action in actions:
            if action['type']=='CANCEL_REQUIRED':self.order(action['intent_id'],'Cancelled')
        return self.reconcile()

    def advance(self,seconds):
        self.wall+=timedelta(seconds=seconds);self.mono+=seconds


class PortfolioTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ledgers=[]
        self.addCleanup(lambda:[l.close() for l in self.ledgers])
        self.fresh()

    def fresh(self,right='C',**policies):
        self.path=self.root/(str(len(self.ledgers))+'.sqlite')
        self.ledger=OfflinePortfolioLedger(self.path,approval(),policy(**policies));self.ledgers.append(self.ledger)
        self.fake=PortfolioFakeBroker(self.ledger);self.fake.start();assert self.fake.reconcile()['matched']
        self.contracts=[replace(CONTRACT,con_id=101+i,strike=6000.+5*i,right=right) for i in range(5)]
        self.ledger.configure(self.fake.generation,[(c,25) for c in self.contracts],6000,self.account(),lambda _:True)
        self.quote_serial=0;self.quotes()

    def account(self,funds='200000'):
        return VerifiedAccountSnapshot(ACCOUNT,ACCOUNT,True,'USD',funds,funds,funds,funds,funds,self.fake.mono)

    def quotes(self,**changes):
        for contract in self.contracts:
            self.quote_serial+=1
            q=LiquidityFixture('quote:'+str(self.quote_serial),
                OptionObservation(contract,contract.con_id,9.9,10.,FeedStatus.LIVE,self.fake.wall,self.fake.mono),
                SPXObservation(6000.,FeedStatus.LIVE,self.fake.wall,self.fake.mono),100,100,self.fake.wall,self.fake.mono,
                '.05',100,100,'.05','.05',OptionGreeks(.48 if contract.right=='C' else -.48,.01,.2,-.5,.25,10.,6000.,0.),self.fake.wall,self.fake.mono)
            q=replace(q,**changes)
            self.ledger.observe_liquidity(self.fake.generation,self.fake.event(),q)

    def schedule(self):return self.ledger.schedule(self.fake.generation,self.account())

    def own(self):
        actions=self.schedule();ids=self.fake.dispatch(actions);self.fake.match();assert self.fake.reconcile()['matched']
        return ids

    def restart(self):
        old=self.ledger;old.close();self.ledgers.remove(old)
        self.ledger=OfflinePortfolioLedger(self.path,approval(),policy(**self.policy_changes) if hasattr(self,'policy_changes') else policy())
        self.ledgers.append(self.ledger);self.fake.ledger=self.ledger
        self.ledger.wall_clock=lambda:self.fake.wall;self.ledger.monotonic_clock=lambda:self.fake.mono
        self.fake.start()

    def test_125_contract_entry_and_liquidation_in_concurrent_five_strike_batches(self):
        actions=self.schedule()
        self.assertEqual(len([a for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']),5)
        self.assertEqual(sum(a['quantity'] for a in actions),125)
        self.assertTrue(all(a['quantity']==25 and not a['executable'] for a in actions))
        ids=self.fake.dispatch(actions);self.fake.advance(.1);self.fake.match()
        self.assertTrue(self.fake.reconcile()['matched'])
        self.assertEqual(self.ledger.positions(),{c.con_id:25 for c in self.contracts})
        self.ledger.trigger_exit('exit-all','EMERGENCY_EXIT_ALL',True)
        exits=self.schedule();self.assertEqual(sum(a.get('quantity',0) for a in exits),125)
        self.fake.dispatch(exits);self.fake.advance(.2);self.fake.match(side='SELL')
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
        self.assertTrue(self.fake.reconcile()['matched'])
        self.assertEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
        metrics=self.ledger.metrics()
        self.assertEqual(len(metrics['strikes']),5)
        self.assertEqual(metrics['portfolio_entry_seconds'],.1)
        self.assertEqual(metrics['portfolio_liquidation_seconds'],.2)
        for row in metrics['strikes']:
            self.assertEqual(row['entry']['fill_percentage'],'100')
            self.assertEqual(row['exit']['fill_percentage'],'100')
            self.assertIsNotNone(row['entry']['first_fill_timestamp'])
            self.assertEqual(Decimal(row['entry']['slippage_vs_arrival_quote']),0)

    def test_displayed_size_never_guarantees_instantaneous_fill(self):
        self.fake.dispatch(self.schedule());self.fake.match(capacity=2)
        self.assertTrue(self.fake.reconcile()['matched'])
        self.assertEqual(sum(self.ledger.positions().values()),10)
        self.assertEqual(sum(self.ledger.reservations('BUY',c.con_id) for c in self.contracts),115)
        self.assertTrue(all(r['entry']['fill_percentage']=='8' for r in self.ledger.metrics()['strikes']))

    def test_adaptive_slices_and_maximum_working_quantities(self):
        self.fresh(max_slice=5,max_working_per_strike=3)
        first=self.schedule()
        self.assertEqual(len(first),15)
        self.assertTrue(all(a['quantity']==5 for a in first))
        self.fake.dispatch(first);self.fake.match();assert self.fake.reconcile()['matched']
        second=self.schedule();self.assertEqual(sum(a['quantity'] for a in second),50)
        self.fake.dispatch(second);self.fake.match();assert self.fake.reconcile()['matched']
        self.assertEqual(sum(self.ledger.positions().values()),125)
        self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in self.schedule()))

    def test_fresh_thin_otm_spreads_missing_greeks_halts_whole_acquisition(self):
        for changes in ({'bid_size':0},{'ask_size':0},{'estimated_exit_capacity':0},
                        {'estimated_entry_slippage':'1'},{'estimated_exit_slippage':'1'},
                        {'greeks':None},{'trading_status':'HALTED'}):
            self.fresh();self.quotes(**changes)
            result=self.schedule()
            self.assertTrue(self.ledger.status()['entry_blocked'],changes)
            self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in result))

    def test_widening_liquidity_cancels_entries_and_preserves_owned_exposure(self):
        self.fake.dispatch(self.schedule());self.fake.match(capacity=2);assert self.fake.reconcile()['matched']
        for cid,data in list(self.ledger.portfolio['quotes'].items()):
            from executor_portfolio_liquidity import fixture_from_payload
            q=fixture_from_payload(data)
            q=replace(q,quote_id='wide:'+cid,option=replace(q.option,bid=9.,ask=11.))
            self.ledger.observe_liquidity(self.fake.generation,self.fake.event(),q)
        result=self.schedule();self.assertEqual(sum(a['type']=='CANCEL_REQUIRED' for a in result),5)
        self.assertEqual(sum(self.ledger.positions().values()),10)
        self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in result))

    def test_central_reservations_prevent_concurrent_double_spend(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            batches=list(pool.map(lambda _:self.schedule(),range(8)))
        proposed=[a for batch in batches for a in batch if a['type']=='SIMULATED_LIMIT_INTENT']
        self.assertEqual(len({a['intent_id'] for a in proposed}),5)
        self.assertLessEqual(sum(self.ledger.reservations('BUY',c.con_id) for c in self.contracts),125)
        self.assertLessEqual(self.ledger._spent()+self.ledger._cash_reserved(),Decimal('200000'))

    def test_aggregate_cash_stop_risk_and_delta_caps_reject_before_entry(self):
        for limits in ({'max_premium_cash':100000},{'max_planned_stop_risk':40000},{'max_delta_notional':1000000}):
            self.fresh(**limits)
            self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in self.schedule()))
            self.assertEqual(self.ledger.state['intents'],{})

    def test_stale_delayed_wrong_and_unsynchronized_quotes_never_authorize(self):
        for kind in ('stale','delayed','sync','wrong'):
            self.fresh()
            from executor_portfolio_liquidity import fixture_from_payload
            q=fixture_from_payload(next(iter(self.ledger.portfolio['quotes'].values())))
            if kind=='stale':self.fake.advance(1.01)
            if kind=='delayed':self.quotes(option=replace(q.option,status=FeedStatus.DELAYED))
            if kind=='sync':
                self.fake.advance(.3)
                self.quotes(sizes_source_time=self.fake.wall-timedelta(seconds=.251))
            if kind=='wrong':
                with self.assertRaises(PaperBrokerError):self.ledger.observe_liquidity(self.fake.generation,self.fake.event(),replace(q,option=replace(q.option,quote_con_id=999)))
                continue
            self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in self.schedule()))

    def test_emergency_during_acquisition_late_fills_and_per_strike_priority(self):
        ids=self.fake.dispatch(self.schedule());self.fake.match(capacity=4);assert self.fake.reconcile()['matched']
        self.ledger.trigger_exit('emergency','EMERGENCY_EXIT_ALL',True)
        self.ledger.trigger_exit('emergency','EMERGENCY_EXIT_ALL',True)
        cancellations=self.schedule()
        self.assertEqual(sum(a['type']=='CANCEL_REQUIRED' for a in cancellations),5)
        self.fake.execution(ids[0],2,price='10')
        # One strike confirms cancellation early; it can liquidate while others wait.
        self.fake.order(ids[0],'Cancelled');assert self.fake.reconcile()['matched']
        actions=self.schedule();sales=[a for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']
        self.assertEqual(len(sales),1);self.assertEqual(sales[0]['quantity'],6)
        self.fake.dispatch(sales);self.fake.match(side='SELL');assert self.fake.reconcile()['matched']
        self.fake.cancel_actions(cancellations)
        actions=self.schedule();self.fake.dispatch(actions);self.fake.match(side='SELL');assert self.fake.reconcile()['matched']
        self.assertEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
        self.assertTrue(self.ledger.status()['entry_blocked'])
        self.assertTrue(all(qty>=0 for qty in self.ledger.positions().values()))

    def test_delayed_ack_timeout_is_uncertain_and_never_retried(self):
        ids=self.fake.dispatch(self.schedule(),ack=False)
        self.fake.advance(2.1);self.quotes()
        actions=self.schedule()
        self.assertTrue(any(a['type']=='RISK_ALERT' for a in actions))
        self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in actions))
        with self.assertRaises(PaperBrokerError):self.ledger.dispatch_batch(self.fake.generation,ids)
        for key in ids:self.fake.order(key,'Cancelled')
        assert self.fake.reconcile()['matched']

    def test_bounded_exit_reprices_keep_original_price_floor(self):
        self.own();self.ledger.trigger_exit('trail','LET_IT_RIDE_REVERSAL')
        first=self.fake.dispatch(self.schedule())
        for attempt in (1,2):
            self.fake.advance(3.1);self.quotes()
            cancels=self.schedule();self.fake.cancel_actions(cancels)
            self.quotes()
            actions=self.schedule();ids=self.fake.dispatch(actions)
            self.assertEqual(len(ids),5)
            for key in ids:
                item=self.ledger.state['intents'][key]
                self.assertEqual(item['attempt'],attempt)
                self.assertGreaterEqual(Decimal(item['limit_price']),Decimal('9.60'))
        self.fake.advance(3.1);self.quotes();self.fake.cancel_actions(self.schedule());self.quotes()
        result=self.schedule()
        self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in result))
        self.assertTrue(any(a['type']=='RISK_ALERT' for a in result))
        self.assertEqual(sum(self.ledger.positions().values()),125)
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')

    def test_exit_liquidity_disappears_and_deadline_persists_risk_without_market_fallback(self):
        self.own();self.ledger.trigger_exit('em','EMERGENCY_EXIT_ALL',True)
        self.fake.advance(21);self.quotes(bid_size=0)
        result=self.schedule()
        self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in result))
        self.assertTrue(self.ledger.portfolio['alerts'])
        self.assertEqual(sum(self.ledger.positions().values()),125)
        self.restart();self.quotes(bid_size=0);assert self.fake.reconcile()['matched']
        self.assertTrue(self.ledger.status()['emergency'])
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')

    def test_competing_exit_triggers_do_not_duplicate_or_short_positions(self):
        self.own()
        self.ledger.trigger_exit('premium','INITIAL_STOP')
        self.ledger.trigger_exit('spx','LET_IT_RIDE_REVERSAL')
        self.ledger.trigger_exit('emergency','EMERGENCY_EXIT_ALL',True)
        first=self.schedule();ids=self.fake.dispatch(first)
        for _ in range(3):
            assert self.fake.reconcile()['matched']
            self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in self.schedule()))
        self.fake.match(side='SELL');assert self.fake.reconcile()['matched']
        self.assertEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
        self.assertEqual(self.ledger.status()['exit_reason'],'EMERGENCY_EXIT_ALL')
        self.assertEqual(len([i for i in self.ledger.state['intents'].values() if i['side']=='SELL']),5)

    def test_rejected_exit_does_not_release_ownership_or_reservation(self):
        self.own();self.ledger.trigger_exit('exit','INITIAL_STOP')
        ids=self.fake.dispatch(self.schedule())
        self.fake.order(ids[0],'Inactive')
        self.assertFalse(self.fake.reconcile()['matched'])
        actions=self.schedule();self.assertTrue(any(a['type']=='CANCEL_REQUIRED' and a['intent_id']==ids[0] for a in actions))
        self.assertEqual(self.ledger.reservations('SELL',101),25)
        self.fake.order(ids[0],'Cancelled');assert self.fake.reconcile()['matched']
        self.assertEqual(self.ledger.reservations('SELL',101),0)

    def test_disconnect_restart_and_prepared_intents_are_recoverable_without_duplicates(self):
        plans=self.schedule();self.restart();self.quotes()
        assert self.fake.reconcile()['matched']
        recovered=self.schedule()
        self.assertEqual({a['intent_id'] for a in recovered},{a['intent_id'] for a in plans})
        ids=self.fake.dispatch(recovered);self.ledger.disconnect(self.fake.generation)
        self.fake.start();self.quotes()
        self.assertTrue(any(i['uncertain'] for i in self.ledger.state['intents'].values()))
        self.assertTrue(self.fake.reconcile()['matched'])
        self.assertFalse(any(i['uncertain'] for i in self.ledger.state['intents'].values()))
        self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in self.schedule()))

    def test_execution_duplicates_corrections_commissions_and_short_evidence_are_quarantined(self):
        ids=self.fake.dispatch(self.schedule())
        record=self.fake.execution(ids[0],5,price='10')
        self.ledger.observe_execution(self.fake.generation,self.fake.event(),record)
        self.assertEqual(self.ledger.positions()[101],5)
        corrected=self.fake.execution(ids[0],4,price='9.95',exec_id=record.exec_id.rsplit('.',1)[0]+'.02',correction_of=record.exec_id)
        assert self.fake.reconcile()['matched']
        self.assertEqual(self.ledger.positions()[101],4)
        self.ledger.trigger_exit('exit','INITIAL_STOP')
        self.fake.cancel_actions(self.schedule());actions=self.schedule();sell=self.fake.dispatch(actions)[0]
        bad=replace(record,exec_id='bad.01',order_ref=sell,order_id=self.ledger.state['intents'][sell]['order_id'],
                    perm_id=self.ledger.state['intents'][sell]['order_id']+1000,side='SELL',quantity=5,price='10')
        with self.assertRaises(PaperBrokerError):self.ledger.observe_execution(self.fake.generation,self.fake.event(),bad)
        self.assertTrue(self.ledger.state['faults'])
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')

    def test_premium_stop_spx_peak_and_no_new_acquisitions_after_exit(self):
        ids=self.fake.dispatch(self.schedule());self.fake.match(capacity=4);assert self.fake.reconcile()['matched']
        for movement in (4,5,20,50):
            self.ledger.observe_spx(self.fake.generation,self.fake.event(),SPXObservation(6000+movement,FeedStatus.LIVE,self.fake.wall,self.fake.mono))
        self.ledger.observe_spx(self.fake.generation,self.fake.event(),SPXObservation(6047,FeedStatus.LIVE,self.fake.wall,self.fake.mono))
        self.assertEqual(self.ledger.portfolio['exit_reason'],'LET_IT_RIDE_REVERSAL')
        self.assertEqual(self.ledger.portfolio['peak'],'6050')
        self.assertTrue(self.ledger.portfolio['ride'])
        self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' and a.get('side')=='BUY' for a in self.schedule()))

    def test_premium_stop_uses_weighted_entry_cost_for_calls_and_puts(self):
        for right in ('C','P'):
            self.fresh(right=right)
            ids=self.fake.dispatch(self.schedule())
            self.fake.execution(ids[0],5,price='9.95')
            self.fake.match();assert self.fake.reconcile()['matched']
            average,stop=self.ledger._premium_stop(101)
            self.assertEqual(average,Decimal('9.99'))
            self.assertEqual(stop,Decimal('6.74'))
            from executor_portfolio_liquidity import fixture_from_payload
            q=fixture_from_payload(self.ledger.portfolio['quotes']['101'])
            self.ledger.observe_liquidity(self.fake.generation,self.fake.event(),replace(q,quote_id='stop-'+right,option=replace(q.option,bid=6.70,ask=6.75)))
            self.assertEqual(self.ledger.portfolio['exit_reason'],'INITIAL_STOP')
            self.assertTrue(self.ledger.status()['entry_blocked'])

    def test_put_spx_symmetry_and_duplicate_event_identity(self):
        self.fresh(right='P');self.own()
        for move in (4,5,20):
            observation=SPXObservation(6000-move,FeedStatus.LIVE,self.fake.wall,self.fake.mono)
            event=self.fake.event()
            self.ledger.observe_spx(self.fake.generation,event,observation)
            self.assertEqual(self.ledger.observe_spx(self.fake.generation,event,observation),'DUPLICATE')
        self.ledger.observe_spx(self.fake.generation,self.fake.event(),SPXObservation(5983,FeedStatus.LIVE,self.fake.wall,self.fake.mono))
        self.assertEqual(self.ledger.portfolio['exit_reason'],'LET_IT_RIDE_REVERSAL')
        self.assertEqual(self.ledger.portfolio['peak'],'5980')

    def test_price_changes_before_dispatch_reprice_without_resetting_arrival_cap(self):
        initial=self.schedule()
        from executor_portfolio_liquidity import fixture_from_payload
        for cid,data in list(self.ledger.portfolio['quotes'].items()):
            q=fixture_from_payload(data)
            self.ledger.observe_liquidity(self.fake.generation,self.fake.event(),replace(q,quote_id='move:'+cid,option=replace(q.option,bid=10.,ask=10.1)))
        with self.assertRaises(PaperBrokerError):self.fake.dispatch(initial)
        abandoned=self.schedule()
        self.assertEqual(sum(a['type']=='LOCAL_ABANDONED' for a in abandoned),5)
        assert self.fake.reconcile()['matched']
        replacement=self.schedule()
        self.assertEqual(len(replacement),5)
        for item in self.ledger.state['intents'].values():
            self.assertEqual(Decimal(item['arrival_price']),Decimal('10'))
            self.assertEqual(Decimal(item['price_bound']),Decimal('10.20'))
        for cid,data in list(self.ledger.portfolio['quotes'].items()):
            q=fixture_from_payload(data)
            self.ledger.observe_liquidity(self.fake.generation,self.fake.event(),replace(q,quote_id='too-far:'+cid,option=replace(q.option,bid=10.2,ask=10.3)))
        self.schedule();assert self.fake.reconcile()['matched']
        self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in self.schedule()))
        self.assertTrue(self.ledger.status()['entry_blocked'])

    def test_emergency_exit_does_not_require_fresh_spx_or_greeks(self):
        self.own();self.fake.advance(2)
        self.quotes(spx=None,greeks=None,greeks_source_time=None,greeks_received_monotonic=None)
        assert self.fake.reconcile()['matched']
        self.ledger.trigger_exit('em','EMERGENCY_EXIT_ALL',True)
        actions=self.schedule()
        self.assertEqual(sum(a.get('quantity',0) for a in actions),125)
        self.fake.dispatch(actions);self.fake.match(side='SELL');assert self.fake.reconcile()['matched']
        self.assertEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')

    def test_late_entry_fills_after_cancelled_evidence_are_reconciled_and_liquidated(self):
        ids=self.fake.dispatch(self.schedule());self.fake.match(capacity=4);assert self.fake.reconcile()['matched']
        self.ledger.trigger_exit('em','EMERGENCY_EXIT_ALL',True)
        self.fake.cancel_actions(self.schedule())
        exits=self.fake.dispatch(self.schedule())
        self.fake.execution(ids[0],2,price='10')
        self.fake.order(ids[0],'Cancelled')
        self.fake.match(side='SELL');assert self.fake.reconcile()['matched']
        self.assertEqual(self.ledger.positions(),{101:2})
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
        self.fake.dispatch(self.schedule());self.fake.match(side='SELL');assert self.fake.reconcile()['matched']
        self.assertEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')

    def test_invalid_numeric_quote_clears_prior_liquidity_and_halts_entries(self):
        from executor_portfolio_liquidity import fixture_from_payload
        q=fixture_from_payload(self.ledger.portfolio['quotes']['101'])
        with self.assertRaises(PaperBrokerError):
            self.ledger.observe_liquidity(self.fake.generation,self.fake.event(),replace(q,quote_id='nan',option=replace(q.option,ask=float('nan'))))
        self.assertNotIn('101',self.ledger.portfolio['quotes'])
        self.assertTrue(self.ledger.status()['entry_blocked'])
        self.assertFalse(any(a['type']=='SIMULATED_LIMIT_INTENT' for a in self.schedule()))

    def test_fresh_buying_power_is_rechecked_at_dispatch(self):
        initial=self.schedule();assert self.fake.reconcile()['matched']
        with self.assertRaises((PaperBrokerError,RuntimeError)):
            self.ledger.dispatch_batch(self.fake.generation,[a['intent_id'] for a in initial],self.account('1000'))
        self.assertTrue(all(i['order_id'] is None for i in self.ledger.state['intents'].values()))

    def test_omitted_uncertain_orders_and_false_flat_snapshots_never_match(self):
        self.fake.dispatch(self.schedule(),ack=False)
        self.assertFalse(self.fake.reconcile(open_orders=(),completed_orders=())['matched'])
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
        for key in self.ledger.state['intents']:self.fake.order(key,'Cancelled')
        assert self.fake.reconcile()['matched']
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')

    def test_emergency_latch_does_not_wait_for_portfolio_scheduler_lock(self):
        import time
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.ledger.lock:
                future=pool.submit(self.ledger.trigger_exit,'urgent','EMERGENCY_EXIT_ALL',True)
                deadline=time.monotonic()+2
                while not self.ledger.emergency.blocked() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(self.ledger.emergency.blocked())
                self.assertTrue(self.ledger.status()['entry_blocked'])
                self.assertFalse(future.done())
            future.result(timeout=2)
        self.assertTrue(self.ledger.status()['emergency'])

    def test_liquidity_and_exposure_diagnostics_are_explicit_estimates(self):
        report=self.ledger.liquidity_report()
        self.assertEqual(len(report['strikes']),5)
        self.assertFalse(report['displayed_liquidity_guaranteed'])
        self.schedule()
        self.assertIsNotNone(self.ledger.status()['last_pretrade_validation'])
        self.assertEqual(self.ledger.liquidity_report()['owned_plus_working_acquisition'],125)
        self.assertLessEqual(Decimal(self.ledger.liquidity_report()['entry_cash_spent_and_reserved']),200000)

    def test_single_strike_executor_and_live_boundaries_remain_unchanged(self):
        path=self.root/'original.sqlite';single=PaperBrokerLedger(path,approval());single.close()
        with self.assertRaises(PaperBrokerError):OfflinePortfolioLedger(path,approval(),policy())
        with self.assertRaises(PaperBrokerError):self.ledger.prepare_intent(None)
        with self.assertRaises(PaperBrokerError):self.ledger.reserve_order_identity(None)
        self.assertFalse(self.ledger.status()['submission_enabled'])
        from safe_sizing import MAX_CONTRACTS
        self.assertEqual(MAX_CONTRACTS,25)


if __name__=='__main__':unittest.main()
