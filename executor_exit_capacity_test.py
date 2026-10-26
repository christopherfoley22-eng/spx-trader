"""Hard capacity, exact prefix sizing, explicit risk/uncertainty and seeded tests."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import random
import unittest
import uuid
import executor_allocation_test as f
from executor_allocation import build_allocation
from executor_exit_capacity import ExitCapacityPolicy,CapacityScenario,evaluate_capacity
from mortificatio_models import ApplicationError,TradeCommand


def capacity_policy(loss='100000',retained='.5'):
    return ExitCapacityPolicy('SYNTHETIC-REVIEW-NOT-ACCOUNT-APPROVAL','.8','.5',loss,(
        CapacityScenario('NORMAL','1','.1','.1','.1',loss),
        CapacityScenario('STRESSED','.75','3.25','.25','.5',loss),
        CapacityScenario('SEVERE',retained,'5','.5','2',loss)))


def make_plan(data=None,policy=None,direction='CALL'):
    return build_allocation(TradeCommand(str(uuid.uuid4()),direction,0),'synthetic-phase11',f.BINDING,
        f.allocation_policy('700000',exit_capacity_policy=policy or capacity_policy()),data or f.evidence(direction,'700000'),f.NOW).report()


class ExitCapacityTest(unittest.TestCase):
    def test_700000_buying_power_is_not_liquidity_capacity(self):
        e=f.evidence(funds='700000');qs=tuple(replace(q,bid_size=50 if q.option.contract.strike in (6000,6005) else 10) for q in e.quotes)
        r=make_plan(replace(e,quotes=qs));self.assertEqual([l['quantity'] for l in r['lines']],[25,25,5,0,0])
        self.assertEqual(r['exit_capacity_admission']['binding_constraint'],'EXIT_CAPACITY_STRIKE_2')
    def test_full_125_and_call_put_symmetry(self):
        for d in ('CALL','PUT'):
            r=make_plan(direction=d);self.assertEqual(r['total_contracts'],125)
            self.assertTrue(all(l['quantity']==25 for l in r['lines']))
            self.assertEqual(r['exit_capacity_admission']['status'],'ADMITTED')
    def test_exact_75_modeled_loss_budget(self):
        r=make_plan(policy=capacity_policy('47347.50'))
        self.assertEqual([l['quantity'] for l in r['lines']],[25,25,25,0,0])
        self.assertEqual(Decimal(r['exit_capacity_admission']['modeled_downside']),Decimal('47347.50'))
        self.assertEqual(r['exit_capacity_admission']['binding_constraint'],'ACCOUNT_MODELED_DOWNSIDE')
    def test_atm_insufficient_never_skips_to_deeper_liquidity(self):
        e=f.evidence(funds='700000');qs=tuple(replace(q,bid_size=18) if q.option.contract.strike==6000 else q for q in e.quotes)
        r=make_plan(replace(e,quotes=qs));self.assertEqual([l['quantity'] for l in r['lines']],[9,0,0,0,0])
    def test_entry_capacity_independent_of_exit_capacity(self):
        e=f.evidence();qs=tuple(replace(q,ask_size=10) if q.option.contract.strike==6000 else q for q in e.quotes)
        r=make_plan(replace(e,quotes=qs));self.assertEqual(r['total_contracts'],8)
        self.assertEqual(r['exit_capacity_admission']['binding_constraint'],'ENTRY_CAPACITY_STRIKE_0')
    def test_different_spreads_and_sudden_expansion_refuse(self):
        e=f.evidence();qs=tuple(replace(q,option=replace(q.option,bid=9.7)) if q.option.contract.strike==6005 else q for q in e.quotes)
        with self.assertRaises(ApplicationError) as c:make_plan(replace(e,quotes=qs),policy=replace(capacity_policy(),max_spread='.2'))
        self.assertEqual(c.exception.admission_decision['reason'],'CAPACITY_SPREAD_LIMIT')
    def test_missing_policy_context_reconciliation_and_protection_fail_closed(self):
        for p,e in ((replace(f.allocation_policy(),exit_capacity_policy=None),f.evidence()),
            (f.allocation_policy(),replace(f.evidence(),capacity_context=None)),
            (f.allocation_policy(),replace(f.evidence(),capacity_context=replace(f.evidence().capacity_context,reconciled=False))),
            (f.allocation_policy(),replace(f.evidence(),capacity_context=replace(f.evidence().capacity_context,protection_feasibility='UNVERIFIED')))):
            with self.assertRaises(ApplicationError):build_allocation(TradeCommand(str(uuid.uuid4()),'CALL',0),'x',f.BINDING,p,e,f.NOW)
    def test_nonpositive_model_stop_price_does_not_invent_proceeds(self):
        r=make_plan(policy=replace(capacity_policy(),scenarios=(CapacityScenario('NORMAL','1','1','0','0','100000'),
            CapacityScenario('STRESSED','.75','10','0','0','100000'),CapacityScenario('SEVERE','.5','20','0','0','100000'))))
        d=r['exit_capacity_admission'];self.assertEqual(d['liquidity'][0]['scenarios']['SEVERE']['modeled_exit_price'],'0')
        self.assertGreater(Decimal(d['modeled_downside']),Decimal('99000'))
    def test_no_liquidity_in_any_strike_and_stale_source_refuse(self):
        e=f.evidence()
        for qs in (tuple(replace(q,bid_size=0) for q in e.quotes),tuple(replace(q,option=replace(q.option,source_time=f.NOW-timedelta(seconds=2))) for q in e.quotes)):
            with self.assertRaises(ApplicationError):make_plan(replace(e,quotes=qs))
    def test_existing_downside_encumbrance_and_inconsistent_stress(self):
        e=f.evidence();e=replace(e,capacity_context=replace(e.capacity_context,existing_modeled_downside='100000'))
        with self.assertRaises(ApplicationError):make_plan(e)
        with self.assertRaises(ApplicationError):replace(capacity_policy(),scenarios=tuple(reversed(capacity_policy().scenarios))).normalized()
    def test_200_seeded_no_oversize_no_skip_risk_monotonicity(self):
        rng=random.Random(11)
        for _ in range(200):
            e=f.evidence();qs=tuple(replace(q,bid_size=rng.randrange(2,101),ask_size=rng.randrange(2,101)) for q in e.quotes)
            e=replace(e,quotes=qs)
            loss=str(rng.randrange(2000,100001))
            try:r=make_plan(e,policy=capacity_policy(loss))
            except ApplicationError:continue
            stopped=False
            for l in r['lines']:
                self.assertLessEqual(l['quantity'],25)
                if stopped:self.assertEqual(l['quantity'],0)
                stopped=stopped or l['quantity']<25
            self.assertLessEqual(r['total_contracts'],125)
            self.assertLessEqual(Decimal(r['exit_capacity_admission']['modeled_downside']),100000)
            self.assertEqual(Decimal(r['reserved_capital'])+Decimal(r['remaining_eligible_capital']),Decimal(r['initial_eligible_capital']))
            # Independent exhaustive 0..125 oracle for these flat-premium
            # fixtures, not an echo of the closed-form sizing implementation.
            by_id={q.option.contract.con_id:q for q in qs};feasible=[]
            for total in range(126):
                vector=[max(0,min(25,total-i*25)) for i in range(5)]
                if total*Decimal('631.30')>Decimal(loss):continue
                if all(n<=int(Decimal(by_id[l['contract']['con_id']].ask_size)*Decimal('.8')) and
                    n<=int(Decimal(by_id[l['contract']['con_id']].bid_size)*Decimal('.5')) for l,n in zip(r['lines'],vector)):
                    feasible.append(total)
            self.assertEqual(r['total_contracts'],max(feasible))


if __name__=='__main__':unittest.main()
