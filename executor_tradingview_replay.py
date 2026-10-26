"""Active SPX profit-rule research for ordered alerts and historical OHLC.

Never creates market observations, entries, fills, order intentions or permits.
Option-premium stops cannot be assessed from SPX candles/alerts alone.
"""
import argparse
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json

from executor_dry_run import spx_profit_decision, PROFIT_PROTECTION_ARM, LET_IT_RIDE_ARM
from executor_tradingview import TradingViewError, price, timestamp
from mortificatio_pre4_experiment import tradingview_csv_to_dataset, _bar


class SPXResearchTrace:
    def __init__(self, direction, entry_spx):
        if direction not in ('CALL', 'PUT'):
            raise TradingViewError('CALL_OR_PUT_REQUIRED')
        self.direction, self.entry = direction, price(entry_spx)
        self.peak, self.ride, self.reason = self.entry, False, None
        self.events = []

    def favorable(self, value):
        return value-self.entry if self.direction == 'CALL' else self.entry-value

    def threshold(self):
        return self.peak + (Decimal('-3') if self.direction == 'CALL' else Decimal('3')) if self.ride else None

    def step(self, value, context):
        if self.reason:
            return
        value = price(value)
        previous_peak, previous_ride = self.peak, self.ride
        old_best = self.favorable(previous_peak)
        favorable, best, self.peak, self.ride, reversal, reason = spx_profit_decision(
            self.direction, self.entry, self.peak, self.ride, value)
        def record(kind, **details):
            self.events.append(dict(context, kind=kind, observed_spx=str(value), favorable_spx_points=str(favorable), **details))
        if self.peak != previous_peak:
            record('FAVORABLE_PEAK', favorable_peak_spx=str(self.peak), favorable_peak_points=str(best))
        if old_best < PROFIT_PROTECTION_ARM <= best:
            record('EARLY_PLUS4_ACTIVATED', reversal_floor_favorable_points='1.25')
        if self.ride and not previous_ride:
            record('PLUS5_TRAILING_ACTIVATED', early_floor_superseded=True)
        if self.ride and (not previous_ride or self.peak != previous_peak):
            record('TRAILING_THRESHOLD_ADVANCED', trailing_threshold_spx=str(self.threshold()))
        if reason:
            self.reason = reason
            record('EXIT_ALL_SIGNAL', reason=reason, reversal_spx_points=str(reversal),
                   triggering_observation_only=True, execution_price=None)

    def result(self):
        return {'direction': self.direction, 'entry_spx': str(self.entry), 'favorable_peak_spx': str(self.peak),
                'trailing_active': self.ride, 'early_protection_active': not self.ride and self.favorable(self.peak) >= PROFIT_PROTECTION_ARM,
                'trailing_threshold_spx': str(self.threshold()) if self.threshold() is not None else None,
                'spx_exit_reason': self.reason, 'events': list(self.events)}


def research_result(state, data_kind, **extra):
    return dict(state.result(), data_kind=data_kind, mode='SPX_RULE_RESEARCH_ONLY', ready=False,
                executable=False, exchange_timestamps_proven=False, continuous_tick_coverage_proven=False,
                initial_premium_stop_evaluated=False, option_pnl=None,
                full_trade_outcome='UNKNOWN_WITHOUT_OPTION_PREMIUM_AND_FILL_EVIDENCE', **extra)


def replay_alert_events(events, direction, entry_spx, entry_time):
    state = SPXResearchTrace(direction, entry_spx)
    entry = timestamp(entry_time)
    previous, seen = None, {}
    for item in events:
        if not isinstance(item, dict) or item.get('symbol') != 'SP:SPX' or item.get('source') != 'TRADINGVIEW_RESEARCH':
            raise TradingViewError('RESEARCH_INBOX_EVENT_REQUIRED')
        stamp = timestamp(item['event_time'])
        identity = item.get('event_id')
        if not isinstance(identity, str) or not identity:
            raise TradingViewError('UNIQUE_EVENT_ID_REQUIRED')
        if identity in seen:
            if item != seen[identity]:
                raise TradingViewError('CHANGED_DUPLICATE_EVENT')
            continue
        seen[identity] = item
        if previous is not None and stamp <= previous:
            raise TradingViewError('UNORDERED_RESEARCH_EVENTS')
        previous = stamp
        if stamp < entry:
            continue
        state.step(item['price'], {'event_id': identity, 'tradingview_event_time': item['event_time'],
                                  'exchange_time': None, 'local_receipt_utc': item.get('local_receipt_utc'),
                                  'local_receipt_monotonic': item.get('local_receipt_monotonic'),
                                  'receipt_process_id': item.get('receipt_process_id'),
                                  'timing': 'ALERT_OBSERVATION_NOT_THRESHOLD_CROSSING_TIME'})
    return research_result(state, 'TRADINGVIEW_SAMPLED_ALERTS', outcome='SPX_EXIT_SIGNAL' if state.reason else 'NO_OBSERVED_SPX_EXIT')


def replay_candles(bars, direction, entry_spx, entry_time):
    state = SPXResearchTrace(direction, entry_spx)
    entry = timestamp(entry_time)
    # Validate the entire dataset, including bars after a hypothetical exit.
    validated, previous = [], None
    for item in bars:
        bar = _bar(item)  # Existing historical parsing only; never its old SPX stop evaluator.
        start = timestamp(bar.timestamp)
        if start.second or start.microsecond or previous is not None and start <= previous:
            raise TradingViewError('ORDERED_ONE_MINUTE_CANDLES_REQUIRED')
        validated.append((bar, start))
        previous = start
    if not validated:
        raise TradingViewError('CANDLES_REQUIRED')
    previous_end, started = None, False
    for bar, start in validated:
        end = start+timedelta(minutes=1)
        if end <= entry:
            continue
        context = {'bar_start': start.isoformat(), 'bar_end': end.isoformat(),
                   'event_time': None, 'exchange_time': None, 'timing': 'UNKNOWN_WITHIN_BAR'}
        if start < entry or not started and start > entry or previous_end is not None and start != previous_end:
            return research_result(state, 'TRADINGVIEW_ONE_MINUTE_OHLC', outcome='UNKNOWN_DATA_WINDOW',
                ambiguity={'reason': 'PARTIAL_ENTRY_BAR_OR_MISSING_BAR_INTERVAL', **context})
        started = True
        previous_end = end
        # Open is the first known bar value; gaps are observed, not interpolated.
        state.step(bar.open, dict(context, event_time=start.isoformat(), timing='BAR_OPEN_OBSERVATION'))
        if state.reason:
            return research_result(state, 'TRADINGVIEW_ONE_MINUTE_OHLC', outcome='SPX_EXIT_AT_BAR_OPEN', ambiguity=None)
        best = state.favorable(state.peak)
        favorable_high = max(state.favorable(bar.high), state.favorable(bar.low))
        favorable_low = min(state.favorable(bar.high), state.favorable(bar.low))
        peak = max(best, favorable_high)
        early_possible = not state.ride and peak >= PROFIT_PROTECTION_ARM and favorable_low <= Decimal('1.25')
        trail_possible = peak >= LET_IT_RIDE_ARM and favorable_low <= peak-Decimal('3')
        if early_possible or trail_possible:
            # OHLC permits repeated interior oscillations. Two extreme-order paths
            # are illustrations only, never an exhaustive ambiguity test.
            paths = []
            for label, extremes in (('HIGH_THEN_LOW', (bar.high, bar.low)), ('LOW_THEN_HIGH', (bar.low, bar.high))):
                branch = deepcopy(state)
                for value in (*extremes, bar.close):
                    branch.step(value, dict(context, assumed_path=label))
                paths.append({'assumption': label, 'illustrative_not_exhaustive': True, **branch.result()})
            close = state.favorable(bar.close)
            certain_reason = ('LET_IT_RIDE_REVERSAL' if state.ride and close <= best-3 else
                              'LET_IT_RIDE_REVERSAL' if not early_possible and peak >= LET_IT_RIDE_ARM and close <= peak-3 else
                              'PROFIT_PROTECTION_FLOOR' if peak < LET_IT_RIDE_ARM and peak >= PROFIT_PROTECTION_ARM and close <= Decimal('1.25') else None)
            state.events.append(dict(context, kind='INTRABAR_AMBIGUITY', possible_early_exit=early_possible,
                                     possible_trailing_exit=trail_possible, exit_guaranteed_by_close_reason=certain_reason,
                                     execution_price=None))
            return research_result(state, 'TRADINGVIEW_ONE_MINUTE_OHLC', outcome='INTRABAR_AMBIGUOUS',
                ambiguity={'reason': 'OHLC_CANNOT_ESTABLISH_TRIGGER_PEAK_OR_EXIT_ORDER', **context,
                           'exit_guaranteed_by_close_reason': certain_reason, 'illustrative_paths': paths})
        # With no possible exit, the favorable extreme was necessarily reached
        # while holding. Its time remains an interval, never an invented tick.
        extreme = bar.high if direction == 'CALL' else bar.low
        state.step(extreme, context)
        state.step(bar.close, dict(context, timing='BAR_CLOSE_VALUE_NO_EXACT_TICK_TIME'))
    return research_result(state, 'TRADINGVIEW_ONE_MINUTE_OHLC', outcome='NO_OBSERVED_SPX_EXIT' if started else 'UNKNOWN_DATA_WINDOW', ambiguity=None)


def replay_csv(path, direction, entry_spx, entry_time, timestamp_unit='unix_seconds'):
    dataset = tradingview_csv_to_dataset(path, entry_time, direction, entry_spx, timestamp_unit)
    result = replay_candles(dataset['entries'][0]['bars'], direction, entry_spx, entry_time)
    result.update(csv_symbol_proven=False, csv_symbol_user_assertion_required='SP:SPX')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', required=True)
    parser.add_argument('--direction', choices=('CALL', 'PUT'), required=True)
    parser.add_argument('--entry-spx', required=True)
    parser.add_argument('--entry-time', required=True)
    parser.add_argument('--timestamp-unit', choices=('iso', 'unix_seconds', 'unix_milliseconds'), default='unix_seconds')
    args = parser.parse_args()
    print(json.dumps(replay_csv(args.csv, args.direction, args.entry_spx, args.entry_time, args.timestamp_unit), sort_keys=True))


if __name__ == '__main__':
    main()
