"""Pure OFFLINE exit sizing. No strategy signals, broker, or entry authority."""
from dataclasses import dataclass,asdict
from datetime import datetime
from decimal import Decimal,ROUND_CEILING
from executor_paper_broker import money,integer,utc,text_id
from executor_paper_types import PaperBrokerError
from simulation_evidence import MAX_SYNC_SECONDS,validate_observation_time


@dataclass(frozen=True)
class AdaptiveExitPolicy:
    version:str
    method:str = 'ADAPTIVE'
    fixed_slice:int = 5
    max_queue:int = 25
    requests_per_window:int = 20
    emergency_reserve:int = 5
    window_seconds:object = '1'
    normal_reprice_seconds:object = '0.50'
    protective_reprice_seconds:object = '0.25'
    emergency_reprice_seconds:object = '0.10'
    cancel_attempts:int = 2
    forecasting_enabled:bool = False

    def normalized(self):
        if not text_id(self.version) or self.method not in {'SINGLE','FIXED','ADAPTIVE','ADAPTIVE_REPRICE'}:
            raise PaperBrokerError('INVALID_ADAPTIVE_POLICY')
        for name,maximum in (('fixed_slice',25),('max_queue',125),('requests_per_window',10000),('emergency_reserve',10000),('cancel_attempts',10)):
            if not integer(getattr(self,name),0 if name=='emergency_reserve' else 1,maximum):raise PaperBrokerError('INVALID_ADAPTIVE_POLICY')
        if self.emergency_reserve>=self.requests_per_window or self.forecasting_enabled is not False:
            raise PaperBrokerError('FORECASTING_DISABLED_OR_INVALID_PACING_RESERVE')
        result=asdict(self)
        for key in ('window_seconds','normal_reprice_seconds','protective_reprice_seconds','emergency_reprice_seconds'):
            result[key]=str(money(result[key],True))
        return result


@dataclass(frozen=True)
class DepthObservation:
    evidence_id:str
    contract:object
    source_time:datetime
    received_monotonic:float
    sequence:int
    bids:tuple  # (Decimal-compatible price, displayed quantity), descending.
    complete:bool = False
    source:str = 'SYNTHETIC_RECORDED_DEPTH'
    origin:str = 'OFFLINE_FIXTURE'

    def payload(self):
        if not text_id(self.evidence_id) or self.origin!='OFFLINE_FIXTURE' or not integer(self.sequence,1) or type(self.complete) is not bool or not text_id(self.source):
            raise PaperBrokerError('INVALID_DEPTH_IDENTITY')
        prices=[];levels=[]
        for price,size in self.bids:
            price=money(price,True)
            if not integer(size,0,1000000):raise PaperBrokerError('INVALID_DEPTH_SIZE')
            prices.append(price);levels.append([str(price),size])
        if not prices or any(a<=b for a,b in zip(prices,prices[1:])):raise PaperBrokerError('DEPTH_NOT_STRICTLY_DESCENDING')
        return dict(evidence_id=self.evidence_id,contract=asdict(self.contract),source_time=utc(self.source_time).isoformat(),
            received_monotonic=self.received_monotonic,sequence=self.sequence,bids=levels,complete=self.complete,source=self.source,origin=self.origin)


def depth_levels(depth,quote,now,mono):
    """No synthetic levels from a missing book; a partial book stays partial."""
    validate_observation_time(datetime.fromisoformat(depth['source_time']),depth['received_monotonic'],now,mono)
    if depth['contract']!=asdict(quote.option.contract) or depth['origin']!='OFFLINE_FIXTURE':raise PaperBrokerError('DEPTH_CONTRACT_MISMATCH')
    stamps=[utc(quote.option.source_time),utc(quote.sizes_source_time),datetime.fromisoformat(depth['source_time'])]
    receipts=[quote.option.received_monotonic,quote.sizes_received_monotonic,depth['received_monotonic']]
    uncertainty=money(quote.source_uncertainty_seconds)
    if (max(stamps)-min(stamps)).total_seconds()+2*float(uncertainty)>MAX_SYNC_SECONDS or max(receipts)-min(receipts)>MAX_SYNC_SECONDS:
        raise PaperBrokerError('DEPTH_SOURCE_SYNCHRONIZATION_FAILED')
    bid=money(quote.option.bid,True);tick=money(quote.min_tick,True)
    levels=[(money(p,True),q) for p,q in depth['bids']]
    if levels[0][0]!=bid or levels[0][1]!=quote.bid_size or any(p%tick for p,_ in levels):
        raise PaperBrokerError('DEPTH_BBO_OR_TICK_DISAGREEMENT')
    return levels


def choose_slices(available,levels,arrival,execution,adaptive,attempt=0):
    """Compare single vs fixed/adaptive slices; all prices retain original floor.

    Expected proceeds are a displayed-depth estimate, never a fill promise. No
    forecast or future observation can delay a valid protective decision.
    """
    tick=money(execution['tick'],True);arrival=money(arrival,True)
    floor=((arrival-money(execution['max_exit_slippage']))/tick).to_integral_value(rounding=ROUND_CEILING)*tick
    if floor<=0:raise PaperBrokerError('NONPOSITIVE_EXIT_PRICE_FLOOR')
    fraction=money(execution['displayed_participation'],True)
    usable=[(money(p,True),int(Decimal(q)*fraction)) for p,q in levels if money(p,True)>=floor]
    capacity=min(available,execution['estimated_capacity'],sum(q for _,q in usable))
    if capacity<=0:return dict(slices=[],floor=str(floor),reason='NO_VALIDATED_CAPACITY_WITHIN_PRICE_BOUND')
    method=adaptive['method'];maximum=execution['max_slice']
    chunk=maximum if method in {'SINGLE','ADAPTIVE','ADAPTIVE_REPRICE'} else min(maximum,adaptive['fixed_slice'])
    slices=[];remaining=capacity;cursor=0;used=0
    while remaining:
        quantity=min(chunk,remaining);need=quantity;cost=Decimal(0);worst=None
        while need and cursor<len(usable):
            price,size=usable[cursor];take=min(need,size-used)
            if take:cost+=price*take;need-=take;used+=take;worst=price
            if used==size:cursor+=1;used=0
        if need:break
        # Adaptive methods can sweep validated depth immediately. Baselines use
        # top-of-book limits; only the repricing baseline lowers them later.
        limit=worst if method.startswith('ADAPTIVE') else usable[0][0]
        if method=='ADAPTIVE_REPRICE':limit=max(floor,limit-money(execution['reprice_step'])*attempt)
        # A top-bid baseline cannot execute at deeper prices below its limit.
        # Its price estimate is conditional on filling at that limit; size and
        # completion remain explicitly uncertain rather than invented proceeds.
        estimate=cost/quantity if method.startswith('ADAPTIVE') else limit
        slices.append(dict(quantity=quantity,limit_price=str(limit),estimated_average=str(cost/quantity),
            estimated_slippage_points=str(arrival-estimate)))
        slices[-1]['estimated_average']=str(estimate)
        remaining-=quantity
    return dict(slices=slices,floor=str(floor),estimated_capacity=capacity,displayed_liquidity_guaranteed=False,
        reason='VALIDATED_VISIBLE_LEVELS' if len(levels)>1 else 'FRESH_BBO_ONLY')


def liquidity_trend(observations):
    """Research only; never called by execution sizing or dispatch eligibility."""
    if len(observations)<2:return dict(status='INSUFFICIENT_EVIDENCE',forecast_enabled=False)
    first,last=observations[0],observations[-1]
    a=sum(q for _,q in first['bids']);b=sum(q for _,q in last['bids'])
    status='DISAPPEARING' if b==0 else 'DECREASING' if b<a else 'INCREASING' if b>a else 'STABLE'
    if money(last['bids'][0][0])<money(first['bids'][0][0]):status='CONCENTRATED_AT_WORSE_PRICES'
    elif len(observations)>=3 and min(sum(q for _,q in x['bids']) for x in observations[1:-1])<min(a,b):status='REPLENISHING'
    return dict(status=status,forecast_enabled=False,confidence='OBSERVED_SAMPLES_ONLY',queue_position_known=False,
        observations=len(observations),complete_depth=all(x['complete'] for x in observations))


def entry_exit_capacity_review(quantity,levels):
    shown=sum(q for _,q in levels)
    return dict(proposed_quantity=quantity,displayed_quantity=shown,displayed_coverage=str(Decimal(shown)/quantity) if quantity else None,
        risk='VISIBLE_DEPTH_BELOW_PROPOSED_SIZE' if shown<quantity else 'DISPLAYED_ONLY_NOT_GUARANTEED',
        entry_gate_active=False,redistribution_authorized=False)


def calendar_review(contract,now,evidence=None):
    if not evidence:return dict(status='UNAVAILABLE_VERIFIED_CALENDAR_REQUIRED',automatic_policy_active=False)
    if (evidence.get('contract')!=asdict(contract) or evidence.get('verified') is not True or not evidence.get('source')
            or not evidence.get('reference')):raise PaperBrokerError('EXACT_VERIFIED_CONTRACT_CALENDAR_REQUIRED')
    opening=utc(datetime.fromisoformat(evidence['open']));closing=utc(datetime.fromisoformat(evidence['close']))
    if opening>=closing:raise PaperBrokerError('INVALID_EXCHANGE_SESSION')
    return dict(status='BEFORE_OPEN' if utc(now)<opening else 'CLOSED_RESIDUAL_REQUIRES_REVIEW' if utc(now)>=closing else 'OPEN',
        seconds_to_close=(closing-utc(now)).total_seconds(),source=evidence['source'],reference=evidence['reference'],
        early_close=evidence.get('early_close'),automatic_policy_active=False)
