"""Pure Decimal five-strike allocation, requiring explicit OFFLINE evidence.

No network, account connection, or order dispatch. Listed chains and authoritative
price timestamps must be supplied by trusted backend fixture providers, not HTTP.
"""
from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from executor_contract_selection import nearby_listed_strikes, atm_delta_rank
from executor_portfolio_liquidity import ExecutionPolicy, LiquidityFixture, assess_liquidity, protected_limit, fixture_payload
from executor_paper_broker import utc, text_id
from executor_paper_types import PaperBrokerError
from executor_market_ingestion import IngestionError
from simulation_evidence import (VerifiedAccountSnapshot, conservative_usable_funds,
    MAX_SYNC_SECONDS, validate_observation_time, positive_number, EvidenceError)
from mortificatio_models import AccountBinding, ApplicationError
from mortificatio_store import encode


def amount(value):
    # Money is never constructed from binary floating-point arithmetic.
    if isinstance(value,bool) or not isinstance(value,(Decimal,str,int)):
        raise ApplicationError('EXACT_DECIMAL_AMOUNT_REQUIRED')
    try:result=Decimal(value)
    except (InvalidOperation,ValueError):raise ApplicationError('INVALID_ALLOCATION_AMOUNT')
    if not result.is_finite() or result<0 or result.adjusted()>15 or result.as_tuple().exponent < -8:
        raise ApplicationError('INVALID_ALLOCATION_AMOUNT')
    return result


@dataclass(frozen=True)
class AllocationPolicy:
    capital_limit: object
    cash_reserve: object
    entry_fee_per_contract: object
    exit_fee_per_contract: object
    minimum_entry_contracts: int
    execution: ExecutionPolicy
    exit_capacity_policy: object = None

    def normalized(self):
        if not isinstance(self.execution,ExecutionPolicy):raise ApplicationError('EXECUTION_POLICY_REQUIRED')
        policy=self.execution.normalized()
        data={name:str(amount(getattr(self,name))) for name in ('capital_limit','cash_reserve','entry_fee_per_contract','exit_fee_per_contract')}
        if (amount(self.capital_limit)<=0 or type(self.minimum_entry_contracts) is not int or not 1<=self.minimum_entry_contracts<=25
                or amount(policy['fee_reserve_per_contract']) < amount(self.entry_fee_per_contract)+amount(self.exit_fee_per_contract)):
            raise ApplicationError('INVALID_CAPITAL_MINIMUM_OR_FEE_RESERVE')
        from executor_exit_capacity import ExitCapacityPolicy
        if self.exit_capacity_policy is not None and not isinstance(self.exit_capacity_policy,ExitCapacityPolicy):
            raise ApplicationError('EXPLICIT_EXIT_CAPACITY_POLICY_REQUIRED')
        return dict(data,minimum_entry_contracts=self.minimum_entry_contracts,execution=policy,
            **({'exit_capacity':self.exit_capacity_policy.normalized()} if self.exit_capacity_policy is not None else {}))


@dataclass(frozen=True)
class ExposureEvidence:
    """External encumbrances exclude this application's central reservations.

    Owned cost EXCLUDES positions backed by central reservations; it reduces
    capital budget, NOT already-net broker available cash.
    Working cash is conservatively deducted even if broker funds already net it.
    Risk and delta are gross non-overlapping owned+external-working exposures.
    """
    evidence_id: str
    binding: AccountBinding
    generation: int
    observed_at: datetime
    received_monotonic: float
    owned_capital: object = '0'
    external_working_capital: object = '0'
    stop_risk: object = '0'
    delta_notional: object = '0'
    complete: bool = True
    excludes_local_reservations: bool = True
    origin: str = 'OFFLINE_FIXTURE'
    outside_spxw_quantity: int = 0
    outside_spxw_working_quantity: int = 0
    outside_spxw_positions_complete: bool = False


@dataclass(frozen=True)
class AllocationEvidence:
    evidence_id: str
    binding: AccountBinding
    generation: int
    expiration: str
    listed_strikes: tuple
    quotes: tuple
    account: VerifiedAccountSnapshot
    exposure: ExposureEvidence
    now_monotonic: float
    chain_complete: bool = True
    origin: str = 'OFFLINE_FIXTURE'
    capacity_context: object = None


@dataclass(frozen=True)
class AllocationPlan:
    """Canonical JSON is immutable; callers receive detached read-only reports."""
    payload_json: str

    def __post_init__(self):
        if encode(json.loads(self.payload_json))!=self.payload_json:
            raise ApplicationError('NONCANONICAL_ALLOCATION_PLAN')

    @property
    def digest(self):return hashlib.sha256(self.payload_json.encode()).hexdigest()

    def report(self):return json.loads(self.payload_json)


def financial_room(config,evidence,binding,now_wall,held_capital='0',held_risk='0',held_delta='0'):
    exposure=evidence.exposure
    if (not isinstance(exposure,ExposureEvidence) or exposure.binding!=binding or exposure.generation!=evidence.generation
            or type(exposure.generation) is not int or not isinstance(exposure.evidence_id,str) or not exposure.evidence_id
            or exposure.origin!='OFFLINE_FIXTURE' or exposure.complete is not True or exposure.excludes_local_reservations is not True):
        raise ApplicationError('COMPLETE_NONOVERLAPPING_EXPOSURE_REQUIRED')
    if (exposure.outside_spxw_positions_complete is not True or type(exposure.outside_spxw_quantity) is not int
            or type(exposure.outside_spxw_working_quantity) is not int or exposure.outside_spxw_quantity!=0
            or exposure.outside_spxw_working_quantity!=0):
        raise ApplicationError('UNACCOUNTED_SPXW_EXPOSURE_BLOCKS_ALLOCATION')
    validate_observation_time(exposure.observed_at,exposure.received_monotonic,now_wall,evidence.now_monotonic)
    funds=min(conservative_usable_funds(evidence.account,binding.account,evidence.now_monotonic),amount(evidence.account.buying_power))
    execution=config['execution']
    cap=min(amount(config['capital_limit']),amount(execution['max_premium_cash']))
    owned,working=amount(exposure.owned_capital),amount(exposure.external_working_capital)
    return (cap-owned-working-amount(held_capital),funds-amount(config['cash_reserve'])-working-amount(held_capital),
        amount(execution['max_planned_stop_risk'])-amount(exposure.stop_risk)-amount(held_risk),
        amount(execution['max_delta_notional'])-amount(exposure.delta_notional)-amount(held_delta))


def _build_allocation(command,trade_id,binding,policy,evidence,now_wall,held_capital='0',held_risk='0',held_delta='0'):
    command.validate();binding.validate();config=policy.normalized();execution=config['execution']
    if (not isinstance(evidence,AllocationEvidence) or evidence.origin!='OFFLINE_FIXTURE'
            or evidence.binding!=binding or type(evidence.generation) is not int or evidence.generation<1
            or evidence.chain_complete is not True or not isinstance(evidence.evidence_id,str) or not evidence.evidence_id
            or not isinstance(evidence.listed_strikes,tuple) or not isinstance(evidence.quotes,tuple)
            or type(evidence.now_monotonic) not in (int,float) or not math.isfinite(evidence.now_monotonic)):
        raise ApplicationError('COMPLETE_ALLOCATION_EVIDENCE_REQUIRED')
    exposure=evidence.exposure;mono=evidence.now_monotonic
    if (not isinstance(exposure,ExposureEvidence) or exposure.binding!=binding or exposure.generation!=evidence.generation
            or exposure.origin!='OFFLINE_FIXTURE' or exposure.complete is not True or exposure.excludes_local_reservations is not True
            or not isinstance(exposure.evidence_id,str) or not exposure.evidence_id):
        raise ApplicationError('COMPLETE_NONOVERLAPPING_EXPOSURE_REQUIRED')
    validate_observation_time(exposure.observed_at,exposure.received_monotonic,now_wall,mono)
    today=utc(now_wall).astimezone(ZoneInfo('America/New_York')).strftime('%Y%m%d')
    if evidence.expiration!=today:raise ApplicationError('CURRENT_DAY_SPXW_CHAIN_REQUIRED')
    strikes=evidence.listed_strikes
    if len(strikes)<5 or any(not positive_number(s) for s in strikes) or len(set(strikes))!=len(strikes):
        raise ApplicationError('COMPLETE_DISTINCT_LISTED_STRIKES_REQUIRED')
    quotes={};by_id={};quote_ids=set();spx=None;times=[];receipts=[];uncertainty=Decimal(0)
    for quote in evidence.quotes:
        if not isinstance(quote,LiquidityFixture):raise ApplicationError('EXACT_QUOTE_REQUIRED')
        contract=quote.option.contract
        if contract.strike not in strikes or contract.expiration!=today:raise ApplicationError('UNLISTED_OR_WRONG_EXPIRATION')
        if contract.strike in quotes or contract.con_id in by_id or quote.quote_id in quote_ids:raise ApplicationError('AMBIGUOUS_CONTRACT_UNIVERSE')
        assess_liquidity(quote,command.direction,now_wall,mono,lambda t:t.strftime('%Y%m%d')==today,execution)
        if spx is not None and fixture_payload(quote)['spx']!=spx:raise ApplicationError('INCONSISTENT_UNDERLYING_EVIDENCE')
        spx=fixture_payload(quote)['spx'];quotes[contract.strike]=quote;by_id[contract.con_id]=quote;quote_ids.add(quote.quote_id)
        times.extend(map(utc,(quote.option.source_time,quote.spx.source_time,quote.sizes_source_time,quote.greeks_source_time)))
        receipts.extend((quote.option.received_monotonic,quote.spx.received_monotonic,quote.sizes_received_monotonic,quote.greeks_received_monotonic))
        uncertainty=max(uncertainty,Decimal(str(quote.source_uncertainty_seconds)))
    if not quotes:raise ApplicationError('MARKET_EVIDENCE_MISSING')
    if (max(times)-min(times)).total_seconds()+float(2*uncertainty)>MAX_SYNC_SECONDS or max(receipts)-min(receipts)>MAX_SYNC_SECONDS:
        raise ApplicationError('ALLOCATION_SOURCE_COHERENCE_FAILED')
    nearby=nearby_listed_strikes(strikes,spx['price'],3)
    if set(nearby)-set(quotes):raise ApplicationError('ATM_CANDIDATE_UNIVERSE_INCOMPLETE')
    atm=min(nearby,key=lambda s:atm_delta_rank(quotes[s].greeks.delta,s,spx['price'],quotes[s].option.contract.con_id))
    ordered=sorted((s for s in strikes if s>atm),reverse=False) if command.direction=='CALL' else sorted((s for s in strikes if s<atm),reverse=True)
    selected=(atm,*ordered[:4])
    if len(selected)!=5 or set(selected)-set(quotes):raise ApplicationError('FIVE_LISTED_STRIKE_QUOTES_REQUIRED')
    held_capital,held_risk,held_delta=map(amount,(held_capital,held_risk,held_delta))
    owned,working,old_risk,old_delta=map(amount,(exposure.owned_capital,exposure.external_working_capital,exposure.stop_risk,exposure.delta_notional))
    cap_room,cash_room,risk_left,delta_left=financial_room(config,evidence,binding,now_wall,held_capital,held_risk,held_delta)
    eligible=max(Decimal(0),min(cap_room,cash_room))
    if risk_left<=0 or delta_left<=0:raise ApplicationError('EXISTING_EXPOSURE_EXCEEDS_RISK_LIMIT')
    remaining=eligible;lines=[];stopped=False;reasons=[]
    fee=amount(execution['fee_reserve_per_contract'])
    estimated_fee=amount(config['entry_fee_per_contract'])+amount(config['exit_fee_per_contract'])
    for index,strike in enumerate(selected):
        q=quotes[strike];ask=Decimal(str(q.option.ask));tick=amount(q.min_tick)
        _,cap=protected_limit('BUY',ask,ask,execution['max_entry_slippage'],execution['reprice_step'],0,tick)
        unit=cap*100+fee
        risk_unit=(Decimal('3.25')+amount(execution['max_exit_slippage']))*100+fee
        delta_unit=(abs(Decimal(str(q.greeks.delta)))*Decimal(str(q.spx.price))*100).quantize(Decimal('.01'),rounding=ROUND_CEILING)
        if ask<=Decimal('3.25'):raise ApplicationError('NONPOSITIVE_INITIAL_PREMIUM_STOP')
        quantity=0 if stopped else min(25,int(remaining//unit),int(risk_left//risk_unit),int(delta_left//delta_unit) if delta_unit else 25)
        if index==0 and quantity<config['minimum_entry_contracts']:raise ApplicationError('MINIMUM_ATM_ENTRY_UNAFFORDABLE')
        reserved=unit*quantity;risk=risk_unit*quantity;delta=delta_unit*quantity
        remaining-=reserved;risk_left-=risk;delta_left-=delta
        reason='PREVIOUS_STRIKE_NOT_FULL' if stopped else 'FULL_STRIKE_ALLOCATION' if quantity==25 else 'CAPITAL_OR_RISK_BOUND'
        stopped=stopped or quantity<25;reasons.append('{}:{}'.format(index,reason))
        lines.append(dict(rank=index,label='ATM' if index==0 else 'OTM+'+str(index),contract=asdict(q.option.contract),quantity=quantity,
            ask=str(ask),delta=str(q.greeks.delta),entry_price_cap=str(cap),premium_cost=str(ask*100*quantity),
            estimated_fees=str(estimated_fee*quantity),reserved_capital=str(reserved),planned_stop_risk=str(risk),
            delta_notional=str(delta),quote_id=q.quote_id,reason=reason))
    total=sum(l['quantity'] for l in lines)
    payload=dict(version=1,mode='OFFLINE_ALLOCATION_NO_EXECUTION',trade_id=trade_id,command_id=command.command_id,
        binding=asdict(binding),generation=evidence.generation,direction=command.direction,expiration=today,
        risk_policy_version=binding.risk_version,created_at=utc(now_wall).isoformat(),entry_spx=str(spx['price']),
        lines=lines,total_contracts=total,initial_eligible_capital=str(eligible),remaining_eligible_capital=str(remaining),
        reserved_capital=str(eligible-remaining),estimated_premium_cost=str(sum((Decimal(l['premium_cost']) for l in lines),Decimal(0))),
        estimated_fees=str(estimated_fee*total),planned_stop_risk=str(sum((Decimal(l['planned_stop_risk']) for l in lines),Decimal(0))),
        delta_notional=str(sum((Decimal(l['delta_notional']) for l in lines),Decimal(0))),policy=config,
        market_evidence=dict(evidence_id=evidence.evidence_id,listed_strikes=list(strikes),chain_complete=True,
            quotes=[fixture_payload(q) for q in evidence.quotes]),
        account_evidence=asdict(evidence.account),exposure_evidence=dict(evidence_id=exposure.evidence_id,
            owned_capital=str(owned),external_working_capital=str(working),stop_risk=str(old_risk),delta_notional=str(old_delta),
            observed_at=utc(exposure.observed_at).isoformat(),received_monotonic=exposure.received_monotonic,
            outside_spxw_quantity=exposure.outside_spxw_quantity,outside_spxw_working_quantity=exposure.outside_spxw_working_quantity,
            outside_spxw_positions_complete=exposure.outside_spxw_positions_complete),
        prior_local_reservations=dict(capital=str(held_capital),stop_risk=str(held_risk),delta_notional=str(held_delta)),
        reasons=reasons,submission_enabled=False)
    # Account monetary fields may be Decimals; canonicalize explicitly.
    for key in ('available_funds','buying_power','excess_liquidity','total_cash_value','settled_cash'):
        payload['account_evidence'][key]=str(amount(payload['account_evidence'][key]))
    from executor_exit_capacity import apply_capacity
    return AllocationPlan(encode(apply_capacity(payload,evidence,now_wall)))


def build_allocation(*args,**kwargs):
    try:return _build_allocation(*args,**kwargs)
    except (ApplicationError,PaperBrokerError,IngestionError,EvidenceError,ValueError,TypeError,AttributeError,KeyError,ArithmeticError) as exc:
        error=exc if isinstance(exc,ApplicationError) else ApplicationError('ALLOCATION_EVIDENCE_REJECTED')
        if not hasattr(error,'admission_decision'):
            command=args[0] if args else kwargs.get('command')
            evidence=args[4] if len(args)>4 else kwargs.get('evidence')
            now=args[5] if len(args)>5 else kwargs.get('now_wall')
            error.admission_decision=dict(status='REJECTED',reason=error.code,
                direction=getattr(command,'direction',None),generation=getattr(evidence,'generation',None),
                decision_at=now.isoformat() if isinstance(now,datetime) else None,
                requested_quantities=None,admitted_quantities=[0]*5,eligible_contracts=None,
                binding_constraint='MARKET_FINANCIAL_OR_IDENTITY_PREFLIGHT',data_quality='INSUFFICIENT',
                estimated_entry_cost=None,modeled_downside=None,scenario_costs=None,
                origin='OFFLINE_PREFLIGHT_FINANCIAL_SIZE_NOT_ESTABLISHED',submission_enabled=False)
        raise error from exc if error is not exc else None
