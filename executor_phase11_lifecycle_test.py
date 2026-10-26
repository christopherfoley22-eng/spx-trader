"""Authenticated hard admission and post-admission liquidity failure injection."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
import unittest
import executor_phase10_lifecycle_test as fixtures
UniqueProvider=fixtures.UniqueProvider
from executor_exit_capacity_test import capacity_policy
from mortificatio_models import ApplicationError
from mortificatio_store import encode
from executor_entry_readiness import StreamingEntryEvidence


class Phase11LifecycleTest(unittest.TestCase):
    for _name in ('setUp','cleanup_lifecycle','enroll','login','grant','authorize','wall','mono','verify','enter','engine','orders','fill','last','restart'):
        locals()[_name]=getattr(fixtures.Phase10LifecycleTest,_name)
    def ready(self):
        self.provider=UniqueProvider(self.provider);self.allocation.provider=self.provider
        self.allocation.policy=replace(self.allocation.policy,exit_capacity_policy=capacity_policy())
        self.allocation.configuration=encode(self.allocation.policy.normalized())
        self.lifecycle.configure_partial_fill_protection(self.service_token,'phase11-obligations-v1')
        return self.lifecycle.prepare_entry_candidates(self.service_token)
    def test_capacity_reduced_plan_reserves_only_admitted_amount(self):
        original=self.provider.snapshot
        def thin(direction):
            e=original(direction);return replace(e,quotes=tuple(replace(q,bid_size=18) if q.option.contract.strike==6000 else q for q in e.quotes))
        self.provider.snapshot=thin;self.ready();r=self.enter();self.assertEqual(r['allocation']['total_contracts'],9)
        self.assertEqual(sum(i['quantity'] for i in self.orders(r)),9)
    def test_quote_deterioration_between_preparation_and_click_blocks(self):
        self.ready();original=self.provider.snapshot
        self.provider.snapshot=lambda d:replace(original(d),quotes=tuple(replace(q,bid_size=10) for q in original(d).quotes))
        with self.assertRaises(ApplicationError):self.enter()
    def test_liquidity_deterioration_after_admission_never_dispatches(self):
        self.ready()
        def hook(stage,trade):
            if stage!='AFTER_CONFIGURATION':return
            original=self.provider.snapshot
            self.provider.snapshot=lambda d:replace(original(d),quotes=tuple(replace(q,quote_id='thin:'+q.quote_id,bid_size=10) for q in original(d).quotes))
        self.lifecycle.fault_hook=hook;r=self.enter()
        self.assertFalse(self.orders(r));self.assertTrue(self.engine(r).portfolio['halted'])
    def test_partial_fill_obligations_survive_duplicate_callback_and_restart(self):
        self.ready();r=self.enter();self.fill(r,1);e=self.engine(r)
        obligations=e.portfolio['protection_obligations'];self.assertEqual(len(obligations),5)
        self.assertTrue(all(o['quantity']==1 and not o['native_protection_verified'] for o in obligations.values()))
        self.assertTrue(e.portfolio['halted']);self.restart();self.lifecycle.supervise(self.service_token)
        recovered=self.engine(r).portfolio['protection_obligations']
        self.assertEqual(set(recovered),set(obligations))
        for identity,old in obligations.items():
            # Broker proof cycles must refresh after reconnect; the economic
            # obligation identity/quantity/premium must survive unchanged.
            self.assertEqual({k:v for k,v in recovered[identity].items() if k!='coverage_as_of_reconciliation'},
                {k:v for k,v in old.items() if k!='coverage_as_of_reconciliation'})
            self.assertTrue(recovered[identity]['coverage_as_of_reconciliation']['snapshot_digest'])
        self.assertFalse(self.last(r)['reconciled_flat'])
    def test_unrelated_receipts_do_not_refresh_frozen_option_quote(self):
        provider=StreamingEntryEvidence(self.mono);current=UniqueProvider(self.provider)
        self.allocation.provider=provider
        provider.publish('CALL',current.snapshot('CALL'));provider.publish('PUT',current.snapshot('PUT'))
        self.lifecycle.prepare_entry_candidates(self.service_token)
        self.now+=2;provider.publish('PUT',current.snapshot('PUT'))
        with self.assertRaises(ApplicationError):self.enter()
    def test_emergency_during_partial_acquisition_preserves_obligations_and_reservations(self):
        self.ready();r=self.enter();self.fill(r,1)
        import uuid
        self.lifecycle.emergency_exit_all(self.session.token,self.session.csrf_token,str(uuid.uuid4()))
        self.assertTrue(self.lifecycle.checkpoint.read()['fenced'])
        self.assertEqual(len(self.engine(r).portfolio['protection_obligations']),5)
        self.assertFalse(self.last(r)['reconciled_flat'])
    def test_missing_capacity_context_is_audited_without_reserving_cash(self):
        original=self.provider.snapshot
        self.provider.snapshot=lambda d:replace(original(d),capacity_context=None)
        with self.assertRaises(ApplicationError):self.enter()
        with self.security.store.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM allocation_reservations').fetchone()[0],0)

    def test_streamed_liquidity_deterioration_cannot_be_hidden_by_provider_snapshot(self):
        self.ready()
        def hook(stage,trade):
            if stage!='AFTER_CONFIGURATION':return
            e=self.lifecycle.engines[trade]
            for cid,payload in list(e.portfolio['quotes'].items()):
                from executor_portfolio_liquidity import fixture_from_payload
                q=replace(fixture_from_payload(payload),quote_id='independent-thin:'+cid,bid_size=10)
                e.observe_liquidity(e.state['generation'],'independent-thin:'+cid,q)
        self.lifecycle.fault_hook=hook;r=self.enter();e=self.engine(r)
        self.assertFalse(self.orders(r));self.assertTrue(e.portfolio['halted'])
        self.assertFalse(e.state['faults'])
        self.assertTrue(all(q['quote_id'].startswith('independent-thin:') for q in e.portfolio['quotes'].values()))

    def test_changed_provider_quote_identity_fences_without_quarantining_economic_ledger(self):
        self.ready()
        def hook(stage,trade):
            if stage!='AFTER_CONFIGURATION':return
            original=self.provider.snapshot
            self.provider.snapshot=lambda d:replace(original(d),quotes=tuple(replace(q,option=replace(q.option,ask=10.1)) for q in original(d).quotes))
        self.lifecycle.fault_hook=hook;r=self.enter();e=self.engine(r)
        self.assertFalse(self.orders(r));self.assertTrue(e.portfolio['halted']);self.assertFalse(e.state['faults'])


if __name__=='__main__':unittest.main()
