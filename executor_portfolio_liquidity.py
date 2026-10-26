"""Explicit OFFLINE liquidity/risk policies. No broker client or execution permit."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from executor_market_ingestion import OptionGreeks, validate_greeks
from executor_paper_broker import integer, money, text_id, utc
from executor_paper_types import PaperBrokerError
from simulation_evidence import (EvidenceClock, OptionObservation, SPXObservation,
    MAX_SYNC_SECONDS, MAX_AGE_SECONDS, MAX_FUTURE_SECONDS, validate_observation_time)

MAX_STRIKES = 5
MAX_PORTFOLIO_CONTRACTS = 125


@dataclass(frozen=True)
class ExecutionPolicy:
    max_spread: object
    max_entry_slippage: object
    max_exit_slippage: object
    displayed_participation: object
    min_slice: int
    max_slice: int
    max_working_per_strike: int
    max_reprices: int
    reprice_step: object
    acknowledgement_timeout: object
    working_timeout: object
    entry_deadline: object
    liquidation_deadline: object
    fee_reserve_per_contract: object
    max_premium_cash: object
    max_planned_stop_risk: object
    max_delta_notional: object

    def normalized(self):
        data = {}
        for name, value in self.__dict__.items():
            if name in {'min_slice','max_slice','max_working_per_strike','max_reprices'}:
                if not integer(value, 0 if name=='max_reprices' else 1, 20 if name=='max_reprices' else 25):
                    raise PaperBrokerError('INVALID_EXECUTION_POLICY')
                data[name] = value
            else:
                amount = money(value)
                if amount < 0 or name not in {'max_entry_slippage','max_exit_slippage','fee_reserve_per_contract'} and amount == 0:
                    raise PaperBrokerError('INVALID_EXECUTION_POLICY')
                data[name] = str(amount)
        if self.min_slice > self.max_slice or not 0 < money(self.displayed_participation) <= 1:
            raise PaperBrokerError('INVALID_EXECUTION_POLICY')
        return data


@dataclass(frozen=True)
class LiquidityFixture:
    quote_id: str
    option: OptionObservation
    spx: SPXObservation
    bid_size: int
    ask_size: int
    sizes_source_time: datetime
    sizes_received_monotonic: float
    min_tick: object
    estimated_entry_capacity: int
    estimated_exit_capacity: int
    estimated_entry_slippage: object
    estimated_exit_slippage: object
    greeks: OptionGreeks
    greeks_source_time: datetime
    greeks_received_monotonic: float
    trading_status: str = 'TRADING'
    source_uncertainty_seconds: object = '0'
    origin: str = 'OFFLINE_FIXTURE'


def assess_liquidity(quote, direction, now_wall, now_mono, session_has_expiration, policy, side='BUY'):
    if not isinstance(quote, LiquidityFixture) or quote.origin != 'OFFLINE_FIXTURE' or not text_id(quote.quote_id):
        raise PaperBrokerError('EXPLICIT_OFFLINE_LIQUIDITY_FIXTURE_REQUIRED')
    if quote.trading_status != 'TRADING':
        raise PaperBrokerError('CONTRACT_NOT_TRADING')
    if (not integer(quote.bid_size,0) or not integer(quote.ask_size,0)
            or not integer(quote.estimated_entry_capacity,0) or not integer(quote.estimated_exit_capacity,0)):
        raise PaperBrokerError('INVALID_DISPLAYED_OR_ESTIMATED_SIZE')
    clock = EvidenceClock()
    if side == 'BUY':
        clock.validate_pair(direction, quote.spx, quote.option, now_wall, now_mono, session_has_expiration)
    else:
        clock.validate_option_risk(direction, quote.option, now_wall, now_mono, session_has_expiration)
    bid, ask = money(quote.option.bid,True), money(quote.option.ask,True)
    tick = money(quote.min_tick,True)
    if bid > ask or ask-bid > money(policy['max_spread']):
        raise PaperBrokerError('CROSSED_OR_EXCESSIVE_SPREAD')
    if bid % tick or ask % tick:
        raise PaperBrokerError('QUOTE_NOT_ON_VERIFIED_TICK_GRID')
    validate_observation_time(quote.sizes_source_time, quote.sizes_received_monotonic, now_wall, now_mono)
    uncertainty = money(quote.source_uncertainty_seconds)
    if uncertainty < 0 or uncertainty > Decimal(str(MAX_SYNC_SECONDS)):
        raise PaperBrokerError('SOURCE_UNCERTAINTY_INVALID')
    sources = [utc(quote.option.source_time),utc(quote.sizes_source_time)]
    receipts = [quote.option.received_monotonic,quote.sizes_received_monotonic]
    if side == 'BUY':
        sources.append(utc(quote.spx.source_time))
        receipts.append(quote.spx.received_monotonic)
        validate_greeks(quote.option.contract, quote.greeks)
        validate_observation_time(quote.greeks_source_time, quote.greeks_received_monotonic, now_wall, now_mono)
        sources.append(utc(quote.greeks_source_time));receipts.append(quote.greeks_received_monotonic)
    if ((max(sources)-min(sources)).total_seconds()+float(2*uncertainty) > MAX_SYNC_SECONDS
            or max(receipts)-min(receipts) > MAX_SYNC_SECONDS
            or any((utc(now_wall)-stamp).total_seconds()+float(uncertainty)>MAX_AGE_SECONDS
                   or (stamp-utc(now_wall)).total_seconds()+float(uncertainty)>MAX_FUTURE_SECONDS for stamp in sources)):
        raise PaperBrokerError('SOURCE_OR_SIZE_SYNCHRONIZATION_FAILED')
    entry_slip, exit_slip = money(quote.estimated_entry_slippage), money(quote.estimated_exit_slippage)
    if min(entry_slip,exit_slip)<0 or exit_slip>money(policy['max_exit_slippage']) or side=='BUY' and entry_slip>money(policy['max_entry_slippage']):
        raise PaperBrokerError('ESTIMATED_SLIPPAGE_EXCEEDS_POLICY')
    fraction = money(policy['displayed_participation'])
    buy_size = min(int(Decimal(quote.ask_size)*fraction),quote.estimated_entry_capacity)
    sell_size = min(int(Decimal(quote.bid_size)*fraction),quote.estimated_exit_capacity)
    if sell_size < policy['min_slice'] or side=='BUY' and buy_size<policy['min_slice']:
        raise PaperBrokerError('INSUFFICIENT_ESTIMATED_ENTRY_OR_EXIT_LIQUIDITY')
    return {'bid':str(bid),'ask':str(ask),'tick':str(tick),'buy_capacity':buy_size,'sell_capacity':sell_size,
            'estimated_entry_slippage':str(entry_slip),'estimated_exit_slippage':str(exit_slip),
            'displayed_bid_size':quote.bid_size,'displayed_ask_size':quote.ask_size,
            'displayed_liquidity_guaranteed':False,'executable':False}


def protected_limit(side, arrival, current, slippage, step, attempt, tick):
    arrival,current,slippage,step,tick=map(money,(arrival,current,slippage,step,tick))
    if side=='BUY':
        cap=((arrival+slippage)/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
        limit=((max(current,arrival+step*attempt))/tick).to_integral_value(rounding=ROUND_CEILING)*tick
        return (limit,cap) if limit<=cap else (None,cap)
    floor=((arrival-slippage)/tick).to_integral_value(rounding=ROUND_CEILING)*tick
    limit=((min(current,arrival-step*attempt))/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
    return (limit,floor) if 0<floor<=limit else (None,floor)


def fixture_payload(quote):
    from dataclasses import asdict
    from enum import Enum
    def plain(value):
        if isinstance(value, datetime): return utc(value).isoformat()
        if isinstance(value, Enum): return value.value
        if isinstance(value, Decimal): return str(value)
        if isinstance(value, dict): return {k:plain(v) for k,v in value.items()}
        if isinstance(value,(tuple,list)): return [plain(v) for v in value]
        return value
    return plain(asdict(quote))


def fixture_from_payload(data):
    from simulation_evidence import VerifiedOptionContract, FeedStatus
    data=dict(data)
    option=dict(data['option']);spx=dict(data['spx']) if data['spx'] else None
    option['contract']=VerifiedOptionContract(**option['contract'])
    for item in (option,*([spx] if spx else [])):
        item['source_time']=datetime.fromisoformat(item['source_time'])
        item['status']=FeedStatus(item['status'])
    data.update(option=OptionObservation(**option),spx=SPXObservation(**spx) if spx else None,
                sizes_source_time=datetime.fromisoformat(data['sizes_source_time']),
                greeks=OptionGreeks(**data['greeks']) if data['greeks'] else None,
                greeks_source_time=datetime.fromisoformat(data['greeks_source_time']) if data['greeks_source_time'] else None)
    return LiquidityFixture(**data)
