"""Fake callback validation only. Transport connect/send never invoked."""
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch
from types import SimpleNamespace as Obj
import executor_ibkr_read_only_test as f
from executor_broker_reality import BrokerRealityValidation
from ibkr_phase11_read_only import RealityProbe,run_read_only
from simulation_evidence import EvidenceError


class BrokerRealityTest(unittest.TestCase):
    identity=f.ReadOnlyValidationTest.identity
    exact_streams=f.ReadOnlyValidationTest.exact_streams
    live=f.ReadOnlyValidationTest.live
    def setUp(self):
        self.now=f.NOW;self.mono=100.;approval,settings=f.config()
        self.validation=BrokerRealityValidation(approval,settings,lambda:self.now,lambda:self.mono)
        self.app=RealityProbe(self.validation)
    def test_live_callback_does_not_prove_quote_timestamp_or_authorize(self):
        r=self.live();self.assertFalse(r['submission_enabled'])
        self.assertEqual(r['source_synchronization'],'NOT_PROVABLE_FROM_RECEIPT_OR_TRADE_TIMESTAMPS')
        self.assertFalse(r['phase11_reality']['broker_native_protection_verified'])
    def test_sizes_have_separate_receipts_and_unbounded_source_age(self):
        options=self.exact_streams();key=next(iter(options))+1000
        self.app.tickSize(key,0,25);row=self.validation.report()['phase11_reality']['size_fields'][0]
        self.assertIsNone(row['source_timestamp']);self.assertIsNone(row['source_age_upper_bound_seconds'])
        self.assertFalse(row['authorizes_entry']);self.assertEqual(row['size'],'25')
        self.now+=timedelta(seconds=2);self.mono+=2
        self.app.tickPrice(f.SPX_QUOTE,4,6001.,None)
        self.assertEqual(self.validation.size_fields[(key,0)]['local_receipt_monotonic'],100.)
    def test_depth_unavailable_is_not_fabricated_and_events_incomplete(self):
        options=self.exact_streams();key=next(iter(options))+1000
        self.assertFalse(self.validation.report()['phase11_reality']['depth_events'])
        self.app.updateMktDepth(key,0,0,1,9.9,12)
        row=self.validation.report()['phase11_reality']['depth_events'][key][0]
        self.assertEqual(row['quality'],'INCOMPLETE_UNTIMESTAMPED_DEPTH');self.assertFalse(row['authorizes_entry'])
    def test_open_stop_observation_never_means_protected(self):
        self.identity();self.validation.begin_summary()
        c=Obj(conId=101)
        o=Obj(account=f.ACCOUNT,orderType='STP',action='SELL',totalQuantity=1,auxPrice=6.75,permId=2,clientId=1,orderRef='stop',parentId=0,ocaType=1)
        self.app.openOrder(1,c,o,Obj(status='Submitted'))
        row=self.validation.report()['phase11_reality']['open_order_details'][0]
        self.assertFalse(row['native_coverage_verified']);self.assertEqual(row['quantity'],'1')
    def test_every_mutation_and_unapproved_connection_remains_disabled(self):
        for method in ('placeOrder','cancelOrder','reqGlobalCancel','exerciseOptions','reqAutoOpenOrders'):
            with self.assertRaises(RuntimeError):getattr(self.app,method)()
        with patch.object(f.transport.ReadOnlyProbe,'connect') as connect:
            with self.assertRaises(EvidenceError):run_read_only(self.validation)
            connect.assert_not_called()
    def test_depth_requires_separate_opt_in_and_exact_qualified_identity(self):
        options=self.exact_streams();key=next(iter(options))+1000
        with patch.object(self.app,'reqMktDepth') as depth:
            with self.assertRaises(EvidenceError):self.app.inspect_depth(key,Obj(conId=123,exchange='SMART'))
            depth.assert_not_called()
    def test_live_unknown_and_ambiguous_accounts_rejected(self):
        for account in ('U1234567','DU9999999',f.ACCOUNT+',DU9999999'):
            a,s=f.config();v=BrokerRealityValidation(a,s,lambda:self.now,lambda:self.mono)
            with self.assertRaises(EvidenceError):v.managed_accounts(account)
            self.assertFalse(v.verified)

    def test_early_index_size_callbacks_do_not_abort_read_only_bootstrap(self):
        self.identity()
        self.app.tickSize(f.SPX_QUOTE,5,0)  # last-size, not bid/ask evidence
        self.assertIsNone(self.validation.failure)
        self.app.contractDetails(f.SPX_DETAILS,Obj(contract=f.spx()))
        self.app.contractDetailsEnd(f.SPX_DETAILS)
        self.app.tickSize(f.SPX_QUOTE,0,0)
        self.assertIsNone(self.validation.failure)
        self.assertFalse(self.validation.size_fields[(f.SPX_QUOTE,0)]['authorizes_entry'])


if __name__=='__main__':unittest.main()
