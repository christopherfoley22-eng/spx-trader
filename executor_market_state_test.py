from dataclasses import replace
from datetime import timedelta
import unittest
from executor_market_state import MarketDatum,MarketState
from executor_paper_broker_test import NOW


class MarketStateTest(unittest.TestCase):
    def setUp(self):self.value=MarketDatum('SYNTHETIC','SPX',1,1,'6000',NOW,NOW,100.,'LIVE')
    def test_source_and_receipt_ages_remain_separate(self):
        x=replace(self.value,receipt_time=NOW+timedelta(seconds=2),receipt_monotonic=102.)
        result=x.assessment(NOW+timedelta(seconds=2),102.)
        self.assertEqual(result['source_age_seconds'],2);self.assertEqual(result['receipt_age_seconds'],0)
        self.assertEqual(result['status'],'STALE_OR_INVALID');self.assertFalse(result['authorizes_entry'])
    def test_missing_timestamps_and_frozen_feed_never_authorize(self):
        for value in (replace(self.value,source_time=None),replace(self.value,quality='FROZEN')):
            self.assertNotEqual(value.assessment(NOW,100.)['status'],'SOURCE_VALIDATED')
    def test_duplicates_and_out_of_order(self):
        state=MarketState();state.ingest(self.value,NOW,100.)
        self.assertEqual(state.ingest(self.value,NOW,100.),'DUPLICATE')
        with self.assertRaises(Exception):state.ingest(replace(self.value,value='6001'),NOW,100.)
        self.assertFalse(state.values)


if __name__=='__main__':unittest.main()
