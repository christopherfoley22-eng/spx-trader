"""Active TradingView SPX rule replay, symmetry and conservative OHLC ambiguity."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from executor_tradingview import TradingViewInbox, TradingViewError
from executor_tradingview_replay import replay_alert_events, replay_candles, replay_csv

NOW=datetime(2026,10,23,14,30,tzinfo=timezone.utc)


def value(direction, favorable):
    return str(Decimal('6000')+(Decimal(str(favorable)) if direction=='CALL' else -Decimal(str(favorable))))


def events(direction,path):
    return [dict(source='TRADINGVIEW_RESEARCH',symbol='SP:SPX',event_id='event-'+str(index),
                 event_time=(NOW+timedelta(seconds=index)).isoformat(),price=value(direction,movement))
            for index,movement in enumerate(path)]


def bar(direction,index,opening,high,low,close):
    actual_high=value(direction,high if direction=='CALL' else low)
    actual_low=value(direction,low if direction=='CALL' else high)
    return dict(timestamp=(NOW+timedelta(minutes=index)).isoformat(),open=value(direction,opening),
                high=actual_high,low=actual_low,close=value(direction,close))


class TradingViewReplayTest(unittest.TestCase):
    def test_early_four_and_four_point_nine_activate_without_profit_taking(self):
        for direction in ('CALL','PUT'):
            for peak in (4,4.9):
                result=replay_alert_events(events(direction,[0,peak,1.25001]),direction,6000,NOW.isoformat())
                self.assertIsNone(result['spx_exit_reason'])
                self.assertTrue(result['early_protection_active'])
                self.assertEqual(sum(e['kind']=='EARLY_PLUS4_ACTIVATED' for e in result['events']),1)
                result=replay_alert_events(events(direction,[0,peak,1.25]),direction,6000,NOW.isoformat())
                self.assertEqual(result['spx_exit_reason'],'PROFIT_PROTECTION_FLOOR')
                self.assertEqual(result['events'][-1]['kind'],'EXIT_ALL_SIGNAL')

    def test_unlimited_trailing_exact_three_points_from_each_new_peak(self):
        for direction in ('CALL','PUT'):
            for peak in (5,10,20,50):
                path=[0,4,4.9,5]+[v for v in (10,20,50) if v<=peak]
                result=replay_alert_events(events(direction,path+[peak-Decimal('2.999')]),direction,6000,NOW.isoformat())
                self.assertIsNone(result['spx_exit_reason'])
                self.assertTrue(result['trailing_active'])
                self.assertFalse(result['early_protection_active'])
                thresholds=[Decimal(e['trailing_threshold_spx']) for e in result['events'] if e['kind']=='TRAILING_THRESHOLD_ADVANCED']
                self.assertEqual(thresholds,sorted(thresholds,reverse=direction=='PUT'))
                result=replay_alert_events(events(direction,path+[peak-3]),direction,6000,NOW.isoformat())
                self.assertEqual(result['spx_exit_reason'],'LET_IT_RIDE_REVERSAL')
                self.assertFalse(result['executable'])
                self.assertFalse(result['initial_premium_stop_evaluated'])
                self.assertIsNone(result['option_pnl'])

    def test_spx_decline_does_not_invent_an_option_premium_stop(self):
        for direction in ('CALL','PUT'):
            result=replay_alert_events(events(direction,[0,-3.25,-20]),direction,6000,NOW.isoformat())
            self.assertIsNone(result['spx_exit_reason'])
            self.assertEqual(result['full_trade_outcome'],'UNKNOWN_WITHOUT_OPTION_PREMIUM_AND_FILL_EVIDENCE')

    def test_event_duplicates_and_bad_order_are_not_silently_replayed(self):
        data=events('CALL',[0,4,1.25])
        first=replay_alert_events(data,'CALL',6000,NOW.isoformat())
        duplicated=replay_alert_events([data[0],data[0],*data[1:]],'CALL',6000,NOW.isoformat())
        self.assertEqual(first,duplicated)
        for altered in ([data[0],dict(data[0],price='6001')],[data[1],data[0]],
                        [data[0],dict(data[1],event_time=data[0]['event_time'])]):
            with self.assertRaises(TradingViewError):
                replay_alert_events(altered,'CALL',6000,NOW.isoformat())
        with self.assertRaises(TradingViewError):
            replay_alert_events([dict(data[0],symbol='SPY')],'CALL',6000,NOW.isoformat())

    def test_inbox_to_active_strategy_replay_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'alerts.sqlite'
            token='SYNTHETIC_RESEARCH_RELAY_TOKEN_123456789'
            now=[NOW]
            inbox=TradingViewInbox(path,token,wall_clock=lambda:now[0],monotonic_clock=lambda:100.)
            for item in events('CALL',[0,4,5,10,20,50,47]):
                now[0]=datetime.fromisoformat(item['event_time'])
                body=dict(item,version=1,source='TRADINGVIEW',stream_id='spx-1m-close-v1',event_time_kind='ALERT_FIRE')
                inbox.ingest(json.dumps(body).encode(),'Bearer '+token)
            inbox=TradingViewInbox(path,token,wall_clock=lambda:now[0],monotonic_clock=lambda:1.)
            result=replay_alert_events(inbox.replay_events(),'CALL',6000,NOW.isoformat())
            self.assertEqual(result['spx_exit_reason'],'LET_IT_RIDE_REVERSAL')
            self.assertEqual(result['favorable_peak_spx'],'6050')
            self.assertTrue(all(e['exchange_time'] is None for e in result['events']))

    def test_unambiguous_candles_record_arms_and_uncapped_peaks(self):
        for direction in ('CALL','PUT'):
            bars=[bar(direction,0,3.5,4,3.5,4),bar(direction,1,4,4.9,4,4.9),bar(direction,2,4.9,5,4.9,5)]
            previous=5
            for movement in [*range(7,50,2),50]:
                bars.append(bar(direction,len(bars),previous,movement,previous,movement))
                previous=movement
            result=replay_candles(bars,direction,6000,NOW.isoformat())
            self.assertEqual(result['outcome'],'NO_OBSERVED_SPX_EXIT')
            self.assertEqual(result['favorable_peak_spx'],value(direction,50))
            kinds=[e['kind'] for e in result['events']]
            self.assertIn('EARLY_PLUS4_ACTIVATED',kinds)
            self.assertIn('PLUS5_TRAILING_ACTIVATED',kinds)
            self.assertTrue(all(e['event_time'] is None for e in result['events'] if e['timing']=='UNKNOWN_WITHIN_BAR'))
            bars.append(bar(direction,len(bars),47.001,47.001,47.001,47.001))
            self.assertIsNone(replay_candles(bars,direction,6000,NOW.isoformat())['spx_exit_reason'])
            bars.append(bar(direction,len(bars),47,47,47,47))
            result=replay_candles(bars,direction,6000,NOW.isoformat())
            self.assertEqual(result['outcome'],'SPX_EXIT_AT_BAR_OPEN')
            self.assertEqual(result['spx_exit_reason'],'LET_IT_RIDE_REVERSAL')

    def test_early_floor_at_known_open_and_no_fixed_profit_cap(self):
        for direction in ('CALL','PUT'):
            bars=[bar(direction,0,3.5,4.9,3.5,4.9),bar(direction,1,1.25,1.25,1.25,1.25)]
            result=replay_candles(bars,direction,6000,NOW.isoformat())
            self.assertEqual(result['spx_exit_reason'],'PROFIT_PROTECTION_FLOOR')
            self.assertEqual(result['outcome'],'SPX_EXIT_AT_BAR_OPEN')

    def test_early_intrabar_order_is_ambiguous_for_both_rights(self):
        for direction in ('CALL','PUT'):
            result=replay_candles([bar(direction,0,3,4.9,1,3)],direction,6000,NOW.isoformat())
            self.assertEqual(result['outcome'],'INTRABAR_AMBIGUOUS')
            self.assertIsNone(result['spx_exit_reason'])
            paths=result['ambiguity']['illustrative_paths']
            self.assertEqual({p['spx_exit_reason'] for p in paths},{None,'PROFIT_PROTECTION_FLOOR'})
            self.assertTrue(all(p['illustrative_not_exhaustive'] for p in paths))

    def test_new_peak_then_reversal_cannot_assume_high_low_order(self):
        for direction in ('CALL','PUT'):
            bars=[bar(direction,0,5,5,5,5),bar(direction,1,5,10,5,10)]
            result=replay_candles(bars,direction,6000,NOW.isoformat())
            self.assertEqual(result['outcome'],'INTRABAR_AMBIGUOUS')
            self.assertEqual(result['favorable_peak_spx'],value(direction,5))
            reasons={p['spx_exit_reason'] for p in result['ambiguity']['illustrative_paths']}
            self.assertEqual(reasons,{None,'LET_IT_RIDE_REVERSAL'})

    def test_matching_extreme_order_paths_do_not_prove_hidden_oscillations(self):
        result=replay_candles([bar('CALL',0,0,8,0,0)],'CALL',6000,NOW.isoformat())
        self.assertEqual(result['outcome'],'INTRABAR_AMBIGUOUS')
        self.assertTrue(result['events'][-1]['possible_early_exit'])
        self.assertEqual({p['spx_exit_reason'] for p in result['ambiguity']['illustrative_paths']},{'LET_IT_RIDE_REVERSAL'})
        # Interior 0 -> +4 -> +1.25 -> +8 -> 0 is also consistent with this OHLC.
        hidden=replay_alert_events(events('CALL',[0,4,1.25,8,0]),'CALL',6000,NOW.isoformat())
        self.assertEqual(hidden['spx_exit_reason'],'PROFIT_PROTECTION_FLOOR')

    def test_guaranteed_exit_by_close_still_has_unknown_intrabar_execution(self):
        result=replay_candles([bar('CALL',0,3.5,4.9,1,1)],'CALL',6000,NOW.isoformat())
        self.assertEqual(result['outcome'],'INTRABAR_AMBIGUOUS')
        self.assertEqual(result['ambiguity']['exit_guaranteed_by_close_reason'],'PROFIT_PROTECTION_FLOOR')
        self.assertIsNone(result['events'][-1]['execution_price'])
        self.assertIsNone(result['events'][-1]['event_time'])

    def test_partial_entry_and_missing_candles_never_invent_peaks(self):
        result=replay_candles([bar('CALL',0,0,50,0,50)],'CALL',6000,(NOW+timedelta(seconds=30)).isoformat())
        self.assertEqual(result['outcome'],'UNKNOWN_DATA_WINDOW')
        self.assertEqual(result['favorable_peak_spx'],'6000')
        self.assertEqual(result['events'],[])
        result=replay_candles([bar('CALL',0,2,3,2,3),bar('CALL',2,3,4,3,4)],'CALL',6000,NOW.isoformat())
        self.assertEqual(result['outcome'],'UNKNOWN_DATA_WINDOW')
        self.assertFalse(result['early_protection_active'])

    def test_invalid_ohlc_order_units_and_direction_are_rejected(self):
        good=bar('CALL',0,0,1,0,1)
        for bars in ([dict(good,high='5999')],[dict(good,low='6002')],[dict(good,close='NaN')],
                     [dict(good,open=True)],[good,good],[]):
            with self.assertRaises((ValueError,TradingViewError)):
                replay_candles(bars,'CALL',6000,NOW.isoformat())
        with self.assertRaises(TradingViewError):
            replay_candles([good],'SELL',6000,NOW.isoformat())

    def test_csv_import_reuses_existing_parser_and_requires_explicit_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'spx.csv'
            path.write_text('time,open,high,low,close,Volume\n'+str(int(NOW.timestamp()*1000))+',6003.5,6004.9,6003.5,6004.9,0\n')
            result=replay_csv(path,'CALL',6000,NOW.isoformat(),'unix_milliseconds')
            self.assertTrue(result['early_protection_active'])
            self.assertFalse(result['initial_premium_stop_evaluated'])
            with self.assertRaises((ValueError,OverflowError,OSError)):
                replay_csv(path,'CALL',6000,NOW.isoformat(),'unix_seconds')


if __name__=='__main__':
    unittest.main()
