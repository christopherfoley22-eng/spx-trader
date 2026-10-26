"""Pure offline allocation invariants using listed, irregular SPXW strike fixtures."""
from dataclasses import replace
from decimal import Decimal
from datetime import timedelta
import random
import unittest
import uuid

from executor_allocation import AllocationPolicy, AllocationEvidence, ExposureEvidence, build_allocation
from executor_portfolio_test import policy
from executor_paper_broker_test import CONTRACT,NOW,ACCOUNT
from executor_portfolio_liquidity import LiquidityFixture
from executor_market_ingestion import OptionGreeks
from simulation_evidence import SPXObservation,OptionObservation,FeedStatus,VerifiedAccountSnapshot
from mortificatio_models import AccountBinding,TradeCommand,ApplicationError
from executor_exit_capacity import ExitCapacityPolicy,CapacityScenario,CapacityContext
from executor_native_protection import ProtectionReadiness
from mortificatio_store import encode
from dataclasses import asdict

BINDING=AccountBinding(ACCOUNT,'PAPER','risk-v1')
STRIKES=(5970.,5980.,5990.,5997.5,6000.,6005.,6012.5,6025.,6050.,6090.)

def sync_protection_plan(data):
    ctx=data.capacity_context
    return replace(data,capacity_context=replace(ctx,protection_plan=ProtectionReadiness(ctx.account,data.generation,
        'FIXTURE-ONLY-PROTECTION-PLAN',encode([asdict(q.option.contract) for q in data.quotes]))))


def allocation_policy(capital='200000',**changes):
    # Explicit synthetic compatibility assumptions, NOT approved account limits.
    capacity=ExitCapacityPolicy('FIXTURE-ONLY-phase11','1','1','1000000',tuple(
        CapacityScenario(name,'1',points,'0','0','1000000')
        for name,points in (('NORMAL','0'),('STRESSED','1'),('SEVERE','2'))))
    return replace(AllocationPolicy(capital,'0','.65','.65',1,policy(),capacity),**changes)


def evidence(direction='CALL',funds='200000',**changes):
    quotes=[]
    for index,strike in enumerate(STRIKES):
        right='C' if direction=='CALL' else 'P'
        contract=replace(CONTRACT,con_id=101+index,strike=strike,right=right)
        delta=min(.99,max(.01,.48+(6000-strike)*.005*(1 if right=='C' else -1)))
        if right=='P':delta=-delta
        quotes.append(LiquidityFixture('allocation-quote:'+str(index),
            OptionObservation(contract,contract.con_id,9.9,10.,FeedStatus.LIVE,NOW,100.),
            SPXObservation(6000.,FeedStatus.LIVE,NOW,100.),100,100,NOW,100.,'.05',100,100,'.05','.05',
            OptionGreeks(delta,.01,.2,-.5,.25,10.,6000.,0.),NOW,100.))
    account=VerifiedAccountSnapshot(ACCOUNT,ACCOUNT,True,'USD',funds,funds,funds,funds,funds,100.)
    data=sync_protection_plan(AllocationEvidence('allocation-evidence',BINDING,1,'20261023',STRIKES,tuple(quotes),account,
        ExposureEvidence('exposure-evidence',BINDING,1,NOW,100.,outside_spxw_positions_complete=True),100.,
        capacity_context=CapacityContext(ACCOUNT,1,NOW,100.,'FIXTURE-RECON','FIXTURE-PROTECTION-FEASIBILITY',
            'SIMULATED_SUPPORTED','0',True)))
    return replace(data,**changes)


class AllocationTest(unittest.TestCase):
    def build(self,capital='200000',direction='CALL',data=None,config=None,**held):
        return build_allocation(TradeCommand(str(uuid.uuid4()),direction,0),str(uuid.uuid4()),BINDING,
            config or allocation_policy(capital),data or evidence(direction),NOW,**held).report()

    def quantities(self,report):return [line['quantity'] for line in report['lines']]

    def test_partial_atm_fifteen_and_no_cheaper_otm_skipping(self):
        report=self.build('15330');self.assertEqual(self.quantities(report),[15,0,0,0,0])
        self.assertEqual(report['reserved_capital'],'15330.00')
        self.assertEqual(Decimal(report['remaining_eligible_capital']),0)

    def test_full_atm_only(self):self.assertEqual(self.quantities(self.build('25550')),[25,0,0,0,0])

    def test_partial_second_strike(self):self.assertEqual(self.quantities(self.build(str(1022*32))),[25,7,0,0,0])

    def test_all_five_and_hard_125_cap(self):
        result=self.build('999999')
        self.assertEqual(self.quantities(result),[25]*5);self.assertEqual(result['total_contracts'],125)
        self.assertEqual(Decimal(result['estimated_premium_cost']),125000)
        self.assertEqual(Decimal(result['estimated_fees']),Decimal('162.50'))
        self.assertEqual(Decimal(result['reserved_capital']),127750)

    def test_irregular_listed_intervals_and_put_call_directions(self):
        for direction,expected in (('CALL',[6000.,6005.,6012.5,6025.,6050.]),('PUT',[6000.,5997.5,5990.,5980.,5970.])):
            report=self.build(direction=direction)
            self.assertEqual([l['contract']['strike'] for l in report['lines']],expected)
            self.assertTrue(all(l['contract']['right']==direction[0] for l in report['lines']))

    def test_delta_target_is_actual_ranking_not_fixed_delta_ladder(self):
        data=evidence();quotes=list(data.quotes)
        # 6005 becomes the best near-ATM .48 candidate, without assuming spacing.
        index=STRIKES.index(6000.);quotes[index]=replace(quotes[index],greeks=replace(quotes[index].greeks,delta=.60))
        index=STRIKES.index(6005.);quotes[index]=replace(quotes[index],greeks=replace(quotes[index].greeks,delta=.481))
        report=self.build(data=replace(data,quotes=tuple(quotes)))
        self.assertEqual(report['lines'][0]['contract']['strike'],6005.)

    def test_insufficient_capital_minimum_entry_and_cash_reserve(self):
        for capital in ('0','1021.99'):
            with self.assertRaises(ApplicationError):self.build(capital)
        with self.assertRaises(ApplicationError):self.build('2044',config=allocation_policy('2044',minimum_entry_contracts=3))
        report=self.build(config=allocation_policy('25550',cash_reserve='190000'))
        self.assertEqual(self.quantities(report),[9,0,0,0,0])

    def test_buying_power_is_independent_and_not_ignored(self):
        data=evidence();data=replace(data,account=replace(data.account,buying_power='15330'))
        self.assertEqual(self.quantities(self.build(data=data)),[15,0,0,0,0])

    def test_owned_exposure_external_orders_and_central_reservations(self):
        data=evidence();data=replace(data,exposure=replace(data.exposure,owned_capital='10000',external_working_capital='2000'))
        result=self.build('50000',data=data,held_capital='10000')
        self.assertEqual(result['initial_eligible_capital'],'28000')
        self.assertEqual(self.quantities(result),[25,2,0,0,0])

    def test_unaccounted_spxw_positions_or_orders_and_unknown_coverage_block_entry(self):
        data=evidence()
        for change in ({'outside_spxw_quantity':1},{'outside_spxw_working_quantity':1},
                       {'outside_spxw_quantity':-1},{'outside_spxw_positions_complete':False}):
            with self.subTest(change=change),self.assertRaises(ApplicationError):
                self.build(data=replace(data,exposure=replace(data.exposure,**change)))

    def test_risk_delta_and_held_risk_budget_are_binding(self):
        config=allocation_policy(execution=policy(max_planned_stop_risk='5355'))
        self.assertEqual(self.quantities(self.build(config=config)),[15,0,0,0,0])
        config=allocation_policy(execution=policy(max_delta_notional='4320000'))
        self.assertEqual(self.quantities(self.build(config=config)),[15,0,0,0,0])
        with self.assertRaises(ApplicationError):self.build(held_risk='50000')

    def test_fees_multiplier_tick_reserve_and_no_float_money(self):
        with self.assertRaises(ApplicationError):allocation_policy(capital=1234.56).normalized()
        with self.assertRaises(ApplicationError):allocation_policy(entry_fee_per_contract='2',exit_fee_per_contract='1').normalized()
        data=evidence();quotes=list(data.quotes)
        quotes[4]=replace(quotes[4],option=replace(quotes[4].option,contract=replace(quotes[4].option.contract,multiplier='10')))
        with self.assertRaises(Exception):self.build(data=replace(data,quotes=tuple(quotes)))
        self.assertEqual(Decimal(self.build('1022')['reserved_capital']),1022)

    def test_missing_chain_strikes_quotes_and_ambiguous_contracts(self):
        data=evidence()
        for bad in (replace(data,listed_strikes=STRIKES[:4]),replace(data,chain_complete=False),
                    replace(data,quotes=data.quotes[:5]),replace(data,quotes=data.quotes+(data.quotes[0],)),
                    replace(data,expiration='20261022'),replace(data,listed_strikes=STRIKES+(6000.,))):
            with self.subTest(bad=bad.evidence_id),self.assertRaises(Exception):self.build(data=bad)

    def test_invalid_market_source_receipt_greeks_feed_and_contracts_fail_closed(self):
        data=evidence();q=data.quotes[4]
        cases=(replace(q,greeks=None),replace(q,trading_status='HALTED'),replace(q,ask_size=0),
               replace(q,option=replace(q.option,status=FeedStatus.DELAYED)),replace(q,option=replace(q.option,status=FeedStatus.FROZEN)),
               replace(q,option=replace(q.option,source_time=None)),replace(q,option=replace(q.option,bid=11.)),
               replace(q,option=replace(q.option,quote_con_id=99999)),
               replace(q,greeks_source_time=NOW-timedelta(seconds=.251)),
               replace(q,option=replace(q.option,source_time=NOW-timedelta(seconds=1.01))))
        for bad in cases:
            quotes=list(data.quotes);quotes[4]=bad
            with self.subTest(bad=str(bad.quote_id)),self.assertRaises(Exception):self.build(data=replace(data,quotes=tuple(quotes)))

    def test_no_skip_when_atm_is_expensive_and_far_otm_is_cheap(self):
        data=evidence();quotes=[]
        for q in data.quotes:
            ask=100. if q.option.contract.strike==6000 else 5.
            quotes.append(replace(q,option=replace(q.option,ask=ask,bid=ask-.1)))
        with self.assertRaises(ApplicationError):self.build('10000',data=replace(data,quotes=tuple(quotes)))

    def test_immutable_plan_and_audit_timestamps_are_distinct(self):
        data=evidence();command=TradeCommand(str(uuid.uuid4()),'CALL',0)
        plan=build_allocation(command,'trade',BINDING,allocation_policy(),data,NOW)
        report=plan.report();report['lines'][0]['quantity']=999
        self.assertEqual(plan.report()['lines'][0]['quantity'],25)
        self.assertIn('source_time',plan.report()['market_evidence']['quotes'][0]['option'])
        self.assertIn('received_monotonic',plan.report()['market_evidence']['quotes'][0]['option'])
        with self.assertRaises(Exception):plan.payload_json='{}'

    def test_seeded_allocation_invariants_for_300_capital_and_direction_cases(self):
        rng=random.Random(61025)
        for _ in range(300):
            capital=Decimal(rng.randrange(102200,19000000))/100
            direction=rng.choice(('CALL','PUT'));report=self.build(str(capital),direction)
            quantities=self.quantities(report);stopped=False
            for quantity in quantities:
                self.assertTrue(0<=quantity<=25)
                if stopped:self.assertEqual(quantity,0)
                stopped=stopped or quantity<25
            self.assertEqual(report['total_contracts'],sum(quantities));self.assertLessEqual(sum(quantities),125)
            reserved=Decimal(report['reserved_capital']);eligible=Decimal(report['initial_eligible_capital'])
            self.assertLessEqual(reserved,eligible)
            self.assertEqual(reserved+Decimal(report['remaining_eligible_capital']),eligible)
            self.assertLessEqual(Decimal(report['planned_stop_risk']),50000)
            self.assertLessEqual(Decimal(report['delta_notional']),50000000)
            if sum(quantities)<125:self.assertLess(Decimal(report['remaining_eligible_capital']),1022)

    def test_200_randomized_prices_fees_reserves_and_risk_boundaries(self):
        rng=random.Random(61026)
        for _ in range(200):
            direction=rng.choice(('CALL','PUT'));data=evidence(direction);quotes=[]
            for quote in data.quotes:
                ask=Decimal(rng.randrange(100,501))*Decimal('.05')
                quotes.append(replace(quote,option=replace(quote.option,ask=float(ask),bid=float(ask-Decimal('.10')))))
            fee=Decimal(rng.randrange(100,301))/100
            config=allocation_policy(str(rng.randrange(5000,150001)),cash_reserve=str(rng.randrange(0,20001)),
                entry_fee_per_contract=str(fee/2),exit_fee_per_contract=str(fee/2),
                execution=policy(fee_reserve_per_contract=str(fee),max_planned_stop_risk=str(rng.randrange(5000,50001))))
            report=self.build(direction=direction,data=replace(data,quotes=tuple(quotes)),config=config)
            quantities=self.quantities(report);first_partial=next((i for i,q in enumerate(quantities) if q<25),5)
            self.assertTrue(all(q==0 for q in quantities[first_partial+1:]))
            self.assertLessEqual(sum(quantities),125)
            total=Decimal(0)
            for line in report['lines']:
                total+=(Decimal(line['entry_price_cap'])*100+fee)*line['quantity']
                self.assertEqual(Decimal(line['premium_cost']),Decimal(line['ask'])*100*line['quantity'])
            self.assertEqual(total,Decimal(report['reserved_capital']))
            self.assertLessEqual(total,Decimal(report['initial_eligible_capital']))
            self.assertLessEqual(Decimal(report['planned_stop_risk']),Decimal(str(config.execution.max_planned_stop_risk)))


if __name__=='__main__':unittest.main()
