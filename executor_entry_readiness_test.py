"""Original allocator equivalence, dual direction identity and fail-closed hints."""
from dataclasses import replace,FrozenInstanceError
from datetime import timedelta
from decimal import Decimal
import unittest
import uuid
from unittest.mock import patch
import executor_allocation_test as fixtures
from executor_entry_readiness import PreparedEntries,StreamingEntryEvidence
from mortificatio_models import TradeCommand,ApplicationError


def evidence(direction='CALL',funds='200000'):
    data=fixtures.evidence(direction,funds)
    if direction=='PUT':
        data=replace(data,quotes=tuple(replace(q,quote_id='P:'+q.quote_id,
            option=replace(q.option,contract=replace(q.option.contract,con_id=q.option.contract.con_id+10000),quote_con_id=q.option.quote_con_id+10000)) for q in data.quotes))
    return fixtures.sync_protection_plan(data)


class EntryReadinessTest(unittest.TestCase):
    def setUp(self):self.ready=PreparedEntries(fixtures.BINDING,fixtures.allocation_policy())
    def confirm(self,direction='CALL',data=None,wall=fixtures.NOW):
        return self.ready.confirm(TradeCommand(str(uuid.uuid4()),direction,0),str(uuid.uuid4()),data or evidence(direction),wall)

    def test_dual_candidates_are_immutable_and_preserve_actual_five_strike_ladders(self):
        call=self.ready.publish('CALL',evidence(),fixtures.NOW,1);put=self.ready.publish('PUT',evidence('PUT'),fixtures.NOW,1)
        self.assertNotEqual(call.plan_id,put.plan_id)
        for direction,c in (('CALL',call),('PUT',put)):
            plan=self.confirm(direction).report();self.assertEqual([x['quantity'] for x in plan['lines']],[25]*5)
            self.assertEqual(Decimal(plan['reserved_capital']),Decimal('127750'))
            with self.assertRaises(FrozenInstanceError):c.sequence=99
        self.assertEqual([x['contract']['strike'] for x in self.confirm('PUT').report()['lines']],[6000.,5997.5,5990.,5980.,5970.])

    def test_click_validates_but_does_not_rank_or_rebuild_unchanged_allocation(self):
        self.ready.publish('CALL',evidence(),fixtures.NOW,1)
        with patch('executor_entry_readiness.build_allocation',side_effect=AssertionError('click must not rebuild')):
            plan=self.confirm();self.assertEqual(plan.report()['total_contracts'],125)

    def test_changed_funds_reservations_quotes_greeks_and_generation_invalidate(self):
        data=evidence()
        cases=[replace(data,account=replace(data.account,buying_power='1000')),replace(data,generation=2),
            replace(data,quotes=(replace(data.quotes[0],ask_size=1),)+data.quotes[1:])]
        for bad in cases:
            with self.subTest(case=bad.evidence_id):
                self.ready.publish('CALL',data,fixtures.NOW,self.ready.sequences.get('CALL',0)+1)
                with self.assertRaises(Exception):self.confirm(data=bad)
                self.assertNotIn('CALL',self.ready.candidates)

    def test_stale_source_and_clock_rollback_not_refreshed_by_recent_receipt(self):
        self.ready.publish('CALL',evidence(),fixtures.NOW,1)
        with self.assertRaises(Exception):self.confirm(data=replace(evidence(),now_monotonic=102.),wall=fixtures.NOW+timedelta(seconds=2))
        self.ready.publish('CALL',evidence(),fixtures.NOW,2)
        with self.assertRaises(Exception):self.confirm(data=replace(evidence(),now_monotonic=99.))

    def test_partial_atm_and_no_skipping_and_reserved_capital(self):
        ready=PreparedEntries(fixtures.BINDING,fixtures.allocation_policy('15330'))
        c=ready.publish('CALL',evidence(),fixtures.NOW,1)
        self.assertEqual([x['quantity'] for x in c.report()['lines']],[15,0,0,0,0])
        with self.assertRaises(Exception):ready.confirm(TradeCommand(str(uuid.uuid4()),'CALL',0),'t',evidence(),fixtures.NOW,('1000','0','0'))

    def test_cross_direction_conid_collision_and_backwards_sequence_fail_closed(self):
        self.ready.publish('CALL',evidence(),fixtures.NOW,1)
        with self.assertRaises(ApplicationError):self.ready.publish('PUT',fixtures.evidence('PUT'),fixtures.NOW,1)
        self.assertFalse(self.ready.candidates)
        self.ready.publish('CALL',evidence(),fixtures.NOW,2)
        with self.assertRaises(ApplicationError):self.ready.publish('CALL',evidence(),fixtures.NOW,1)

    def test_stream_snapshot_only_advances_observation_clock_not_source_evidence(self):
        provider=StreamingEntryEvidence(lambda:103.);data=evidence();provider.publish('CALL',data)
        current=provider.snapshot('CALL');self.assertEqual(current.now_monotonic,103.)
        self.assertEqual(current.quotes[0].option.source_time,data.quotes[0].option.source_time)
        self.assertEqual(current.quotes[0].option.received_monotonic,100.)


if __name__=='__main__':unittest.main()
