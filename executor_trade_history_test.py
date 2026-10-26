"""Real journal/fill/fee accounting and finalization through the offline lifecycle."""
from dataclasses import replace
from decimal import Decimal
from datetime import timedelta
import unittest
import uuid

import executor_lifecycle_test as fixtures
from executor_allocation_service import AllocationBoundary
from executor_allocation_test import allocation_policy
from executor_paper_types import CommissionRecord
from simulation_evidence import SPXObservation,FeedStatus


class HistoryTest(unittest.TestCase):
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

    def one_strike(self):
        self.allocation=AllocationBoundary(allocation_policy('15330'),self.provider)
        self.core.allocation=self.allocation;self.lifecycle.allocation=self.allocation

    def detail(self,result):return self.lifecycle.history.detail(self.session.token,result['trade_id'])

    def exit_strategy(self,result):
        sign=1 if result['allocation']['direction']=='CALL' else -1
        for price in (6000+4*sign,6000+1.25*sign):
            self.now=(self.wall()+timedelta(milliseconds=100)).timestamp()
            self.lifecycle.observe_spx(self.service_token,result['trade_id'],str(uuid.uuid4()),SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))

    def complete(self,direction='CALL',quantity=1,exit_price='11'):
        result=self.enter(direction);self.fill(result,quantity);self.exit_strategy(result)
        self.fill(result,side='SELL',price=exit_price)
        return result

    def test_five_strike_actual_recorded_premiums_and_fees_equal_portfolio(self):
        result=self.complete(quantity=2);report=self.detail(result)
        self.assertEqual(report['status'],'FINALIZED',report)
        self.assertEqual(report['total_contracts'],10);self.assertEqual(report['strike_count'],5)
        self.assertEqual(Decimal(report['totals']['gross_realized_pnl']),1000)
        self.assertEqual(Decimal(report['totals']['commissions_and_fees']),Decimal('6.50'))
        self.assertEqual(Decimal(report['totals']['net_realized_pnl']),Decimal('993.50'))
        for field in ('gross_realized_pnl','commissions_and_fees','net_realized_pnl'):
            self.assertEqual(sum(Decimal(line[field]) for line in report['per_strike']),Decimal(report['totals'][field]))
        self.assertTrue(all(Decimal(line['weighted_average_entry'])==10 for line in report['per_strike']))
        self.assertTrue(all(Decimal(line['weighted_average_exit'])==11 for line in report['per_strike']))
        summary=self.lifecycle.history.summary(self.session.token)
        self.assertEqual(summary['wins'],1);self.assertEqual(summary['losses'],0)
        self.assertEqual(summary['win_rate_percent'],'100')

    def test_one_strike_weighted_partial_fills_and_fees_are_per_execution(self):
        self.one_strike();result=self.enter('PUT');item=self.orders(result)[0]
        self.fixture.fill(item['order_ref'],1,'9',self.wall(),'weighted-entry.01')
        self.fixture.fill(item['order_ref'],1,'10',self.wall(),'weighted-entry-two.01')
        self.lifecycle.supervise(self.service_token);self.exit_strategy(result)
        exit_item=self.orders(result,'SELL')[0]
        self.fixture.fill(exit_item['order_ref'],1,'10',self.wall(),'weighted-exit.01')
        self.fixture.fill(exit_item['order_ref'],1,'11',self.wall(),'weighted-exit-two.01')
        self.lifecycle.supervise(self.service_token)
        report=self.detail(result)
        self.assertEqual(report['status'],'FINALIZED',report)
        line=report['per_strike'][0]
        self.assertEqual(Decimal(line['weighted_average_entry']),Decimal('9.5'))
        self.assertEqual(Decimal(line['weighted_average_exit']),Decimal('10.5'))
        self.assertEqual(Decimal(line['net_realized_pnl']),Decimal('197.40'))
        self.assertEqual(report['total_contracts'],2);self.assertEqual(report['direction'],'PUT')

    def test_losing_trade_and_breakeven_summary_use_recorded_prices(self):
        result=self.enter();self.fill(result,1)
        for quote in self.provider.snapshot('CALL').quotes:
            if str(quote.option.contract.con_id) in self.engine(result).portfolio['targets']:
                identity=str(uuid.uuid4())
                self.lifecycle.observe_liquidity(self.service_token,result['trade_id'],identity,
                    replace(quote,quote_id=identity,option=replace(quote.option,bid=9.,ask=9.1)))
        self.exit_strategy(result);self.fill(result,side='SELL',price='9')
        report=self.detail(result)
        self.assertEqual(report['status'],'FINALIZED',report)
        self.assertEqual(Decimal(report['totals']['net_realized_pnl']),Decimal('-506.50'))
        summary=self.lifecycle.history.summary(self.session.token)
        self.assertEqual(summary['wins'],0);self.assertEqual(summary['losses'],1)
        self.assertEqual(Decimal(summary['average_net_realized_pnl']),Decimal('-506.50'))

    def test_missing_commission_never_produces_final_zero_or_estimate(self):
        result=self.enter();self.fill(result,1);self.exit_strategy(result)
        identities=[]
        for item in self.orders(result,'SELL'):
            identity=str(uuid.uuid4())+'.01';identities.append(identity)
            self.fixture.fill(item['order_ref'],item['quantity'],'11',self.wall(),identity,fee=None)
        self.lifecycle.supervise(self.service_token)
        report=self.detail(result)
        self.assertEqual(report['status'],'PENDING_RECONCILIATION');self.assertIsNone(report['totals'])
        self.assertEqual(self.lifecycle.history.summary(self.session.token)['total_completed_trades'],0)
        for identity in identities:self.fixture.commission(result['trade_id'],CommissionRecord('fee:'+identity,identity,'.65'))
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(self.detail(result)['status'],'FINALIZED')

    def test_reservation_and_lifecycle_state_are_mandatory_finalization_evidence(self):
        result=self.complete();self.assertEqual(self.detail(result)['status'],'FINALIZED')
        with self.security.store.database() as db:db.execute("UPDATE allocation_reservations SET state='HELD' WHERE trade_id=?",(result['trade_id'],))
        report=self.detail(result)
        self.assertEqual(report['status'],'PENDING_RECONCILIATION');self.assertIsNone(report['totals'])
        self.assertTrue(report['prior_finalization_revoked'])
        self.assertEqual(self.lifecycle.history.summary(self.session.token)['total_completed_trades'],0)

    def test_late_fill_revokes_finalization_before_callback_delivery_and_recalculates(self):
        result=self.complete();original=self.detail(result)
        item=self.orders(result)[0]
        self.fixture.fill(item['order_ref'],1,'10',self.wall(),'late-after-final.01')
        pending=self.detail(result)
        self.assertEqual(pending['status'],'PENDING_RECONCILIATION');self.assertIsNone(pending['totals'])
        self.assertTrue(pending['prior_finalization_revoked'])
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(self.detail(result)['status'],'PENDING_RECONCILIATION')
        self.fill(result,side='SELL',price='11')
        updated=self.detail(result)
        self.assertEqual(updated['status'],'FINALIZED',updated)
        self.assertEqual(updated['total_contracts'],6)
        self.assertGreater(Decimal(updated['totals']['net_realized_pnl']),Decimal(original['totals']['net_realized_pnl']))
        self.assertGreater(updated['revision'],original['revision'])

    def test_execution_correction_replaces_old_price_and_commission_revision(self):
        result=self.complete();old=self.detail(result)
        entry=next(e['record'] for e in self.engine(result).state['executions'].values() if e['record']['side']=='BUY')
        source=self.engine(result).portfolio['fill_source_times'][entry['exec_id']]['executed_at']
        from datetime import datetime
        new_id=entry['exec_id'].rsplit('.',1)[0]+'.02'
        self.fixture.fill(entry['order_ref'],entry['quantity'],'9',datetime.fromisoformat(source),new_id,fee=None,correction_of=entry['exec_id'])
        self.assertIsNone(self.detail(result)['totals'])
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(self.detail(result)['status'],'PENDING_RECONCILIATION')
        self.fixture.commission(result['trade_id'],CommissionRecord('corrected-fee',new_id,'.65'))
        self.lifecycle.supervise(self.service_token)
        corrected=self.detail(result)
        self.assertEqual(corrected['status'],'FINALIZED',corrected)
        self.assertEqual(Decimal(corrected['totals']['net_realized_pnl']),Decimal(old['totals']['net_realized_pnl'])+100)
        self.assertEqual(Decimal(corrected['totals']['commissions_and_fees']),Decimal(old['totals']['commissions_and_fees']))
        self.assertTrue(any(not e['active'] and e['exec_id']==entry['exec_id'] for e in corrected['execution_history']))

    def test_latest_commission_report_replaces_prior_charge_and_can_record_rebate(self):
        result=self.complete();before=self.detail(result)
        identity=next(iter(self.engine(result).state['executions']))
        old_report=self.engine(result).state['commissions'][identity]['report_id']
        self.fixture.commission(result['trade_id'],CommissionRecord('corrected-charge',identity,'2.25',correction_of=old_report))
        self.assertEqual(self.detail(result)['status'],'PENDING_RECONCILIATION')
        self.lifecycle.supervise(self.service_token)
        changed=self.detail(result)
        self.assertEqual(changed['status'],'FINALIZED',changed)
        self.assertEqual(Decimal(changed['totals']['commissions_and_fees']),Decimal(before['totals']['commissions_and_fees'])+Decimal('1.60'))
        self.fixture.commission(result['trade_id'],CommissionRecord('explicit-rebate',identity,'-.25',correction_of='corrected-charge'))
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(Decimal(self.detail(result)['totals']['commissions_and_fees']),Decimal(before['totals']['commissions_and_fees'])-Decimal('.90'))

    def test_restart_keeps_revisions_and_requires_reverified_terminal_evidence(self):
        result=self.complete();before=self.detail(result)
        self.restart();after=self.detail(result)
        self.assertEqual(after['status'],'FINALIZED',after)
        self.assertEqual(before['totals'],after['totals'])
        self.assertGreaterEqual(after['revision'],before['revision'])
        with self.security.store.database() as db:self.assertGreater(db.execute('SELECT count(*) FROM trade_history_revisions').fetchone()[0],0)

    def test_disconnected_or_stale_account_proof_revokes_current_finalization(self):
        result=self.complete();self.assertEqual(self.detail(result)['status'],'FINALIZED')
        self.now+=1.01
        pending=self.detail(result)
        self.assertEqual(pending['status'],'PENDING_RECONCILIATION');self.assertIsNone(pending['totals'])
        self.lifecycle.supervise(self.service_token)
        self.assertEqual(self.detail(result)['status'],'FINALIZED')
        self.lifecycle.disconnect(self.service_token)
        self.assertEqual(self.detail(result)['status'],'PENDING_RECONCILIATION')

    def test_chart_uses_only_actual_source_observations_and_fill_markers(self):
        result=self.complete();report=self.detail(result);chart=report['chart']
        self.assertEqual(report['status'],'FINALIZED',report)
        self.assertEqual(chart['status'],'AVAILABLE',chart)
        self.assertIsNone(chart['candles']);self.assertFalse(chart['interpolation_performed'])
        self.assertEqual(len(chart['execution_markers']),10)
        self.assertTrue(all(marker['source']=='OFFLINE_FIXTURE' for marker in chart['execution_markers']))
        self.assertTrue(all('source_time' in point for point in chart['observations']))

    def test_missing_chart_history_is_explicit_and_does_not_fabricate_prices(self):
        result=self.enter();self.fill(result,1)
        self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        self.fill(result,side='SELL',price='11')
        report=self.detail(result)
        self.assertEqual(report['status'],'FINALIZED',report)
        self.assertEqual(report['chart']['status'],'UNAVAILABLE')
        self.assertEqual(report['chart']['reason'],'INSUFFICIENT_RECORDED_SPX_HISTORY')
        self.assertIsNone(report['chart']['candles'])

    def test_projection_tampering_fails_closed(self):
        result=self.complete();self.detail(result)
        with self.security.store.database() as db:db.execute("UPDATE trade_history SET payload='{}'")
        from mortificatio_models import ApplicationError
        with self.assertRaises(ApplicationError):self.detail(result)

    def test_history_gets_do_not_dispatch_cancel_or_reconcile(self):
        result=self.enter();engine=self.engine(result)
        before=(engine.sequence,self.lifecycle.anchor.sequence,len(self.fixture.callbacks_after(0)))
        self.detail(result);self.lifecycle.history.summary(self.session.token);self.lifecycle.history.list_trades(self.session.token,{'state':'ALL'})
        self.assertEqual(before,(engine.sequence,self.lifecycle.anchor.sequence,len(self.fixture.callbacks_after(0))))

    def test_history_projection_failure_never_disables_independent_protection(self):
        result=self.enter();self.fill(result,1)
        self.detail(result)
        with self.security.store.database() as db:db.execute("UPDATE trade_history SET payload='{}'")
        from mortificatio_models import ApplicationError
        with self.assertRaises(ApplicationError):self.detail(result)
        self.assertEqual(self.lifecycle.history.last_error,'HISTORY_REPORTING_UNAVAILABLE')
        self.exit_strategy(result)
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)

    def test_later_emergency_does_not_relabel_an_already_closed_trade(self):
        result=self.complete();before=self.detail(result)
        self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        after=self.detail(result)
        self.assertEqual(self.engine(result).portfolio['exit_reason'],'EMERGENCY_EXIT_ALL')
        self.assertEqual(after['status'],'FINALIZED',after)
        self.assertEqual(after['exit_reason'],before['exit_reason'])
        self.assertTrue(all(line['exit_reason']==before['exit_reason'] for line in after['per_strike']))
        self.assertEqual(after['totals'],before['totals'])

    def test_cancelled_zero_fill_request_does_not_fabricate_completed_performance(self):
        result=self.enter()
        self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        report=self.detail(result)
        self.assertEqual(report['status'],'CANCELLED_NO_FILL',report)
        self.assertIsNone(report['totals'])
        self.assertEqual(self.lifecycle.history.summary(self.session.token)['total_completed_trades'],0)

    def test_equal_timestamp_conflicting_spx_prices_are_not_fabricated_into_a_chart(self):
        result=self.enter();self.fill(result,1)
        for identity,price in (('conflict-arm',6004.),('conflict-exit',6001.25)):
            self.lifecycle.observe_spx(self.service_token,result['trade_id'],identity,SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))
        self.fill(result,side='SELL',price='11')
        report=self.detail(result)
        self.assertEqual(report['status'],'FINALIZED',report)
        self.assertEqual(report['chart']['status'],'UNAVAILABLE')
        self.assertEqual(report['chart']['reason'],'AMBIGUOUS_SOURCE_TIMESTAMP')
        self.assertTrue(all(marker['spx_price'] is None for marker in report['chart']['execution_markers']))

    def test_decimal_cashflow_invariants_on_100_seeded_actual_execution_vectors(self):
        from copy import deepcopy
        import hashlib,random
        from executor_trade_accounting import closed_accounting
        from mortificatio_store import encode
        result=self.complete();original=self.engine(result).state;rng=random.Random(61029)
        for _ in range(100):
            state=deepcopy(original);gross=Decimal(0);fees=Decimal(0)
            quantities={cid:rng.randrange(1,26) for cid in state['portfolio']['targets']}
            for identity,item in state['executions'].items():
                record=item['record'];quantity=quantities[str(record['contract']['con_id'])]
                price=Decimal(rng.randrange(100,401))*Decimal('.05');fee=Decimal(rng.randrange(-25,226))/100
                record['quantity']=quantity;record['price']=str(price)
                state['commissions'][identity]['amount']=str(fee)
                state['portfolio']['fill_source_times'][identity]['record_digest']=hashlib.sha256(encode(record).encode()).hexdigest()
                gross+=price*quantity*100*(1 if record['side']=='SELL' else -1);fees+=fee
            report=closed_accounting(state)
            self.assertEqual(Decimal(report['gross_realized_pnl']),gross)
            self.assertEqual(Decimal(report['commissions_and_fees']),fees)
            self.assertEqual(Decimal(report['net_realized_pnl']),gross-fees)
            self.assertEqual(sum(Decimal(line['net_realized_pnl']) for line in report['per_strike']),gross-fees)


if __name__=='__main__':unittest.main()
