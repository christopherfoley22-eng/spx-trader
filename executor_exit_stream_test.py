"""Synthetic subscription callbacks use authenticated independent protection."""
from dataclasses import replace
import unittest
from threading import Event
from unittest.mock import patch
import executor_liquidation_lifecycle_test as fixtures
from executor_exit_stream import OfflineProtectiveStream,ProtectiveStreamEvent
from executor_paper_types import PaperBrokerError
from simulation_evidence import SPXObservation,FeedStatus
from mortificatio_models import ApplicationError


class ExitStreamTest(unittest.TestCase):
    for _name in ('setUp','cleanup_lifecycle','enroll','login','grant','authorize','wall','mono','verify',
                  'enter','engine','orders','fill','last','restart','configure'):
        locals()[_name]=getattr(fixtures.AdaptiveLifecycleTest,_name)

    def ready(self,capacity=64):
        result=self.enter();self.configure(result);self.fill(result,1)
        self.lifecycle.enable_exit_readiness(self.service_token,result['trade_id'])
        return result,OfflineProtectiveStream(self.lifecycle,self.service_token,result['trade_id'],capacity)

    def event(self,price,sequence,key=None):
        return ProtectiveStreamEvent('SPX',key or 'spx:'+str(sequence),'SPX',sequence,
            SPXObservation(price,FeedStatus.LIVE,self.wall(),self.mono()))

    def test_stream_manifest_and_asynchronous_option_updates_are_read_only(self):
        result,s=self.ready();self.assertEqual(len(s.manifest()),6)
        q=self.provider.snapshot('CALL').quotes[4];cid=q.option.contract.con_id
        s.enqueue(ProtectiveStreamEvent('QUOTE','option:1',str(cid),1,replace(q,quote_id='option:1')))
        s.drain_one();self.assertEqual(self.engine(result).portfolio['quotes'][str(cid)]['quote_id'],'option:1')
        self.assertFalse(self.orders(result,'SELL'))

    def test_stream_trailing_survives_expired_dashboard_and_duplicate_events(self):
        result,s=self.ready();self.security.logout(self.session.token,self.session.csrf_token)
        e=self.event(6050,1);self.assertEqual(s.enqueue(e),'QUEUED');self.assertEqual(s.enqueue(e),'DUPLICATE')
        s.drain_one();self.assertEqual(s.enqueue(e),'DUPLICATE')
        s.enqueue(self.event(6047,2));s.drain_one()
        self.assertEqual(sum(i['quantity'] for i in self.orders(result,'SELL')),5)
        self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])

    def test_out_of_order_and_changed_event_fail_closed(self):
        result,s=self.ready();s.enqueue(self.event(6001,2));s.drain_one();s.enqueue(self.event(6002,1))
        with self.assertRaises(PaperBrokerError):s.drain_one()
        self.assertTrue(self.engine(result).portfolio['halted'])
        with self.assertRaises(PaperBrokerError):s.enqueue(self.event(6003,2))

    def test_emergency_priority_and_overflow_persist_fence_without_claiming_flat(self):
        result,s=self.ready(capacity=1);s.enqueue(self.event(6000,1))
        emergency=ProtectiveStreamEvent('EMERGENCY','stream-panic','PORTFOLIO',1)
        with self.assertRaises(PaperBrokerError):s.enqueue(emergency)
        self.assertTrue(self.authority.emergency.blocked())
        self.assertTrue(self.lifecycle.checkpoint.read()['fenced'])
        self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])

    def test_emergency_processed_before_ordinary_data_and_no_entry_authority(self):
        result,s=self.ready();s.enqueue(self.event(6000,1))
        s.enqueue(ProtectiveStreamEvent('EMERGENCY','prioritized','PORTFOLIO',1));s.drain_one()
        self.assertTrue(self.engine(result).portfolio['emergency']);self.assertEqual(len(s.queue),1)
        with self.assertRaises(ApplicationError):OfflineProtectiveStream(self.lifecycle,self.session.token,result['trade_id'])

    def test_stale_source_is_not_replaced_with_receipt_time(self):
        result,s=self.ready();event=self.event(6050,1);self.now+=3;s.enqueue(event)
        with self.assertRaises(Exception):s.drain_one()
        self.assertFalse(self.orders(result,'SELL'));self.assertTrue(s.failure)

    def test_feed_disconnect_invalidates_hints_and_fences_entries_without_fake_broker_disconnect(self):
        result,s=self.ready();s.disconnect()
        self.assertFalse(self.engine(result).exit_readiness.templates)
        self.assertTrue(self.lifecycle.checkpoint.read()['fenced']);self.assertTrue(self.fixture.connected)
        self.assertFalse(self.lifecycle._trade_status(result['trade_id'])['reconciled_flat'])

    def test_event_worker_wakes_on_callback_without_polling_and_shuts_down(self):
        result,s=self.ready();done=Event();original=self.lifecycle.observe_spx
        def observed(*args):
            out=original(*args);done.set();return out
        self.addCleanup(s.stop_worker)
        with patch.object(self.lifecycle,'observe_spx',side_effect=observed):
            s.start_worker();s.enqueue(self.event(6050,1));self.assertTrue(done.wait(3))
        s.stop_worker();self.assertFalse(s.running);self.assertFalse(s.worker.is_alive())
        self.assertEqual(self.engine(result).portfolio['peak'],'6050')


if __name__=='__main__':unittest.main()
