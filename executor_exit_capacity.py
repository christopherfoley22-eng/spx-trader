"""Hard, Decimal-only acquisition admission. Model evidence is NOT a fill guarantee.

No network/order authority. No default risk budget or liquidity assumptions.
The caller supplies explicit reviewed policy and current account-bound evidence.
"""
from dataclasses import dataclass,asdict
from decimal import Decimal,ROUND_FLOOR
import hashlib
from executor_paper_broker import utc,text_id
from executor_portfolio_liquidity import assess_liquidity
from simulation_evidence import validate_observation_time
from mortificatio_models import ApplicationError
from mortificatio_store import encode


@dataclass(frozen=True)
class CapacityScenario:
    name:str
    retained_liquidity:object
    adverse_premium_points:object
    latency_seconds:object
    adverse_points_per_second:object
    max_loss_dollars:object


@dataclass(frozen=True)
class ExitCapacityPolicy:
    version:str
    entry_realization:object
    max_spread:object
    max_model_loss:object
    scenarios:tuple

    def normalized(self):
        from executor_allocation import amount
        if not isinstance(self.version,str) or not self.version or not isinstance(self.scenarios,tuple) or len(self.scenarios)!=3:
            raise ApplicationError('EXPLICIT_EXIT_CAPACITY_POLICY_REQUIRED')
        data=asdict(self)
        for k in ('entry_realization','max_spread','max_model_loss'):data[k]=str(amount(data[k]))
        if not 0<amount(data['entry_realization'])<=1 or min(amount(data[k]) for k in ('max_spread','max_model_loss'))<=0:
            raise ApplicationError('INVALID_EXIT_CAPACITY_POLICY')
        rows=[]
        for expected,s in zip(('NORMAL','STRESSED','SEVERE'),self.scenarios):
            if not isinstance(s,CapacityScenario) or s.name!=expected:raise ApplicationError('THREE_ORDERED_CAPACITY_SCENARIOS_REQUIRED')
            row=asdict(s)
            for k in row:
                if k!='name':row[k]=str(amount(row[k]))
            if not 0<amount(row['retained_liquidity'])<=1 or amount(row['max_loss_dollars'])<=0:
                raise ApplicationError('INVALID_CAPACITY_SCENARIO')
            rows.append(row)
        for a,b in zip(rows,rows[1:]):
            if (amount(b['retained_liquidity'])>amount(a['retained_liquidity']) or
                amount(b['adverse_premium_points'])<amount(a['adverse_premium_points']) or
                amount(b['latency_seconds'])<amount(a['latency_seconds']) or
                amount(b['adverse_points_per_second'])<amount(a['adverse_points_per_second'])):
                raise ApplicationError('STRESS_MUST_NOT_IMPROVE_ASSUMPTIONS')
        data['scenarios']=rows;return data


@dataclass(frozen=True)
class CapacityContext:
    account:str
    generation:int
    observed_at:object
    received_monotonic:float
    reconciliation_ref:str
    protection_feasibility_ref:str
    protection_feasibility:str
    existing_modeled_downside:object
    reconciled:bool
    origin:str = 'OFFLINE_FIXTURE'
    protection_plan:object = None


def evaluate_capacity(report,evidence,now_wall):
    """Largest strict 25-prefix <= financially eligible target, or explicit refusal.

    Current displayed BBO/estimated capacity are discounted independently under
    configured scenarios; no invented depth/replenishment or known queue position.
    Full premium is charged on model-unliquidated residuals. This is a static
    conditional stress model, not a guarantee that a protective stop will fill.
    """
    from executor_allocation import amount
    config=report['policy'].get('exit_capacity')
    ctx=getattr(evidence,'capacity_context',None)
    result=dict(direction=report['direction'],generation=evidence.generation,decision_at=utc(now_wall).isoformat(),
        requested_quantities=[x['quantity'] for x in report['lines']],admitted_quantities=[0]*len(report['lines']),
        eligible_contracts=[x['contract'] for x in report['lines']],status='REJECTED',binding_constraint=None,
        data_quality='INSUFFICIENT',scenario_costs={},estimated_entry_cost='0',modeled_downside='0',
        origin='OFFLINE_MODEL_NOT_BROKER_OBSERVATION',submission_enabled=False)
    try:
        if not config:raise ApplicationError('EXIT_CAPACITY_POLICY_MISSING')
        # Revalidate even persisted/reconstructed policy; callers cannot inject
        # unvalidated dictionaries to gain authority.
        policy=ExitCapacityPolicy(config['version'],config['entry_realization'],config['max_spread'],config['max_model_loss'],
            tuple(CapacityScenario(**x) for x in config['scenarios']))
        config=policy.normalized()
        if (not isinstance(ctx,CapacityContext) or ctx.origin!='OFFLINE_FIXTURE' or ctx.reconciled is not True
            or ctx.account!=report['binding']['account'] or type(ctx.generation) is not int or ctx.generation!=evidence.generation
            or not text_id(ctx.reconciliation_ref) or not text_id(ctx.protection_feasibility_ref) or ctx.protection_feasibility!='SIMULATED_SUPPORTED'):
            raise ApplicationError('RECONCILIATION_AND_PROTECTION_FEASIBILITY_REQUIRED')
        validate_observation_time(ctx.observed_at,ctx.received_monotonic,now_wall,evidence.now_monotonic)
        from executor_native_protection import ProtectionReadiness
        if not isinstance(ctx.protection_plan,ProtectionReadiness):raise ApplicationError('MANDATORY_PROTECTION_READINESS_REQUIRED')
        readiness=ctx.protection_plan.validate(ctx.account,evidence.generation,[q.option.contract for q in evidence.quotes])
        result['protection_plan']=readiness
        existing=amount(ctx.existing_modeled_downside)
        # Existing managed/external stop-risk and local holds cannot vanish from
        # this additional modeled-loss budget. The larger disclosed encumbrance
        # is used; sources must declare non-overlap as in the original allocator.
        encumbered=max(existing,amount(evidence.exposure.stop_risk))+amount(report['prior_local_reservations']['stop_risk'])
        result.update(policy_version=config['version'],policy=config,reconciliation_ref=ctx.reconciliation_ref,
            protection_feasibility='SIMULATED_ONLY_REAL_BROKER_UNVERIFIED',data_quality='SOURCE_VALIDATED_CURRENT_BBO_ONLY',
            existing_downside_encumbrance=str(encumbered))
        by_id={q.option.contract.con_id:q for q in evidence.quotes};rows=[]
        for line in report['lines']:
            q=by_id.get(line['contract']['con_id'])
            if q is None or asdict(q.option.contract)!=line['contract']:raise ApplicationError('CAPACITY_EXACT_CONTRACT_REQUIRED')
            observed=assess_liquidity(q,report['direction'],now_wall,evidence.now_monotonic,
                lambda t:t.strftime('%Y%m%d')==report['expiration'],report['policy']['execution'])
            bid,ask=amount(str(q.option.bid)),amount(str(q.option.ask))
            if line['quantity'] and ask>amount(line['entry_price_cap']):raise ApplicationError('CAPACITY_ENTRY_PRICE_BOUND_CHANGED')
            if ask<=bid or ask-bid>amount(config['max_spread']):raise ApplicationError('CAPACITY_SPREAD_LIMIT')
            entry=int((Decimal(min(q.ask_size,q.estimated_entry_capacity))*amount(config['entry_realization'])).to_integral_value(rounding=ROUND_FLOOR))
            scenarios={}
            for s in config['scenarios']:
                capacity=int((Decimal(min(q.bid_size,q.estimated_exit_capacity))*amount(s['retained_liquidity'])).to_integral_value(rounding=ROUND_FLOOR))
                mark=max(Decimal(0),bid-amount(s['adverse_premium_points'])-amount(s['latency_seconds'])*amount(s['adverse_points_per_second']))
                scenarios[s['name']]=dict(capacity=capacity,modeled_exit_price=str(mark))
            rows.append(dict(contract=line['contract'],quote_id=q.quote_id,bid=str(bid),ask=str(ask),
                displayed_bid=q.bid_size,displayed_ask=q.ask_size,entry_capacity=entry,scenarios=scenarios,
                source_time=utc(q.option.source_time).isoformat(),receipt_monotonic=q.option.received_monotonic,
                source_uncertainty_seconds=str(q.source_uncertainty_seconds),history='CURRENT_OBSERVATION_ONLY_NO_INFERRED_REPLENISHMENT'))
        result['liquidity']=rows
        def costs(quantities):
            totals={s['name']:dict(liquidation_cost=Decimal(0),downside=Decimal(0),residual=0) for s in config['scenarios']}
            for line,row,n in zip(report['lines'],rows,quantities):
                cap=amount(line['entry_price_cap']);fees=(amount(report['policy']['entry_fee_per_contract'])+amount(report['policy']['exit_fee_per_contract']))*n
                for name,v in totals.items():
                    scenario=row['scenarios'][name];filled=min(n,scenario['capacity']);residual=n-filled
                    proceeds=amount(scenario['modeled_exit_price'])*100*filled
                    loss=max(Decimal(0),cap*100*n-proceeds)+fees
                    v['liquidation_cost']+=loss;v['downside']+=loss;v['residual']+=residual
            return totals
        requested=result['requested_quantities'];admitted=[0]*len(rows);limiting='FINANCIAL_OR_125_CAP'
        used={s['name']:Decimal(0) for s in config['scenarios']}
        for index,(line,row) in enumerate(zip(report['lines'],rows)):
            # Linear fixed-price stress assumptions permit an exact closed-form
            # bound. No search/recomputation of all strikes per extra contract.
            bounds=[(line['quantity'],'FINANCIAL_OR_125_CAP'),(row['entry_capacity'],'ENTRY_CAPACITY_STRIKE_'+str(index)),
                (min(s['capacity'] for s in row['scenarios'].values()),'EXIT_CAPACITY_STRIKE_'+str(index))]
            units={}
            for s in config['scenarios']:
                name=s['name'];mark=amount(row['scenarios'][name]['modeled_exit_price'])
                unit=max(Decimal(0),(amount(line['entry_price_cap'])-mark)*100)+amount(report['policy']['entry_fee_per_contract'])+amount(report['policy']['exit_fee_per_contract'])
                units[name]=unit
                for budget,reason in ((config['max_model_loss'],'ACCOUNT_MODELED_DOWNSIDE'),(s['max_loss_dollars'],'SCENARIO_LOSS_BUDGET')):
                    available=max(Decimal(0),amount(budget)-encumbered-used[name])
                    bounds.append((int(available//unit) if unit else 25,reason))
            n,reason=min(bounds,key=lambda x:x[0]);admitted[index]=max(0,n)
            if n<line['quantity']:limiting=reason
            for name,unit in units.items():used[name]+=unit*admitted[index]
            if admitted[index]<25:break
        final=costs(admitted)
        result.update(admitted_quantities=admitted,binding_constraint=limiting,
            estimated_entry_cost=str(sum(((amount(l['ask'])*100+amount(report['policy']['entry_fee_per_contract']))*n for l,n in zip(report['lines'],admitted)),Decimal(0))),
            maximum_entry_premium_cost=str(sum((amount(l['entry_price_cap'])*100*n for l,n in zip(report['lines'],admitted)),Decimal(0))),
            scenario_costs={name:{k:str(v) if isinstance(v,Decimal) else v for k,v in d.items()} for name,d in final.items()},
            requested_scenario_costs={name:{k:str(v) if isinstance(v,Decimal) else v for k,v in d.items()} for name,d in costs(requested).items()},
            modeled_downside=str(max(x['downside'] for x in final.values())),
            status='ADMITTED' if admitted==requested else 'REDUCED',reason=limiting)
        if sum(admitted)<report['policy']['minimum_entry_contracts']:
            result.update(status='REJECTED',reason='CAPACITY_MINIMUM_ENTRY_NOT_MET')
    except Exception as exc:
        # The result is inspectable but cannot become entry authority.
        result.update(status='REJECTED',reason=str(exc) if isinstance(exc,ApplicationError) else 'CAPACITY_EVIDENCE_INVALID')
    result['decision_id']=hashlib.sha256(encode(result).encode()).hexdigest()
    return result


def require_capacity(report,evidence,now_wall,resize=False):
    decision=evaluate_capacity(report,evidence,now_wall)
    if decision['status']=='REJECTED' or not resize and decision['admitted_quantities']!=[l['quantity'] for l in report['lines']]:
        exc=ApplicationError('HARD_EXIT_CAPACITY_ADMISSION_REJECTED');exc.admission_decision=decision;raise exc
    return decision


def apply_capacity(report,evidence,now_wall):
    from executor_allocation import amount
    decision=require_capacity(report,evidence,now_wall,resize=True)
    for line,n in zip(report['lines'],decision['admitted_quantities']):
        old=line['quantity']
        for key in ('premium_cost','estimated_fees','reserved_capital','planned_stop_risk','delta_notional'):
            line[key]=str(amount(line[key])*n/old if old else Decimal(0))
        line['quantity']=n
        if n<old:line['reason']='HARD_EXIT_CAPACITY_REDUCTION'
    for key,line_key in (('reserved_capital','reserved_capital'),('estimated_premium_cost','premium_cost'),('estimated_fees','estimated_fees'),('planned_stop_risk','planned_stop_risk'),('delta_notional','delta_notional')):
        report[key]=str(sum((amount(l[line_key]) for l in report['lines']),Decimal(0)))
    report['total_contracts']=sum(l['quantity'] for l in report['lines'])
    report['remaining_eligible_capital']=str(amount(report['initial_eligible_capital'])-amount(report['reserved_capital']))
    report['exit_capacity_admission']=decision
    return report
