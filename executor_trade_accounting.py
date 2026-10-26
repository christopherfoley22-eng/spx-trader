"""Decimal closed-trade accounting from replayed executions, never Greeks.

Execution corrections replace superseded economic revisions. Fee totals use the
latest USD commission report for each ACTIVE execution revision, matching the
existing broker ledger's correction model. Inactive reports remain audit evidence.
"""
from datetime import datetime
from decimal import Decimal,localcontext
import hashlib

from executor_paper_broker import money,utc,integer
from mortificatio_models import ApplicationError
from mortificatio_store import encode


def number(value):return format(value,'f')


def closed_accounting(state):
    with localcontext() as context:
        context.prec=50
        return _closed_accounting(state)


def _closed_accounting(state):
    portfolio=state['portfolio'];targets=portfolio['targets'];source_times=portfolio.get('fill_source_times',{})
    lines={};histories=[];markers=[]
    for cid,target in targets.items():
        contract=target['contract']
        if contract['multiplier']!='100' or contract['currency']!='USD' or contract['trading_class']!='SPXW':
            raise ApplicationError('HISTORY_CONTRACT_ACCOUNTING_INVALID')
        lines[cid]=dict(contract=contract,direction='CALL' if contract['right']=='C' else 'PUT',entry_quantity=0,exit_quantity=0,
            entry_value=Decimal(0),exit_value=Decimal(0),fees=Decimal(0),entries=[],exits=[])
    for identity,item in state['executions'].items():
        execution=item['record'];cid=str(execution['contract']['con_id'])
        if cid not in lines or execution['contract']!=lines[cid]['contract']:raise ApplicationError('HISTORY_EXECUTION_IDENTITY_INVALID')
        source=source_times.get(identity)
        if source is None or source['record_digest']!=hashlib.sha256(encode(execution).encode()).hexdigest():
            raise ApplicationError('HISTORY_SOURCE_EXECUTION_TIME_REQUIRED')
        stamp=utc(datetime.fromisoformat(source['executed_at']))
        histories.append(dict(execution,active=item['active'],source_time=stamp.isoformat(),time_basis='RECORDED_SOURCE_EXECUTION_TIME'))
        if not item['active']:continue
        fee=state['commissions'].get(identity)
        if fee is None or fee['currency']!='USD':raise ApplicationError('HISTORY_COMMISSION_EVIDENCE_REQUIRED')
        line=lines[cid];line['fees']+=money(fee['amount'])
        if execution['bust']:continue
        quantity=execution['quantity'];price=money(execution['price'],True)
        if not integer(quantity,1,25):raise ApplicationError('HISTORY_EXECUTION_QUANTITY_INVALID')
        if execution['side']=='BUY':
            line['entry_quantity']+=quantity;line['entry_value']+=price*quantity;line['entries'].append(stamp)
        elif execution['side']=='SELL':
            line['exit_quantity']+=quantity;line['exit_value']+=price*quantity;line['exits'].append(stamp)
        else:raise ApplicationError('HISTORY_EXECUTION_SIDE_INVALID')
        markers.append(dict(exec_id=identity,source_time=stamp.isoformat(),side=execution['side'],quantity=quantity,
            contract=execution['contract'],option_premium=number(price),spx_price=None,source='OFFLINE_FIXTURE'))
    result=[];entry_times=[];exit_times=[]
    for cid,line in lines.items():
        entered,exited=line['entry_quantity'],line['exit_quantity']
        if entered!=exited or entered>25:raise ApplicationError('HISTORY_STRIKE_NOT_FLAT')
        if line['entries'] and (not line['exits'] or min(line['exits'])<min(line['entries'])):
            raise ApplicationError('HISTORY_EXECUTION_TIME_ORDER_INVALID')
        gross=(line['exit_value']-line['entry_value'])*100
        entry_times+=line['entries'];exit_times+=line['exits']
        result.append(dict(contract=line['contract'],strike=line['contract']['strike'],direction=line['direction'],
            entry_quantity=entered,exit_quantity=exited,
            weighted_average_entry=number(line['entry_value']/entered) if entered else None,
            weighted_average_exit=number(line['exit_value']/exited) if exited else None,
            entry_premium_value=number(line['entry_value']),exit_premium_value=number(line['exit_value']),
            gross_realized_pnl=number(gross),commissions_and_fees=number(line['fees']),net_realized_pnl=number(gross-line['fees']),
            entry_time=min(line['entries']).isoformat() if line['entries'] else None,
            exit_time=max(line['exits']).isoformat() if line['exits'] else None,
            exit_reason=portfolio['exit_reason'],reconciliation_status='RECONCILED'))
    gross=sum((Decimal(line['gross_realized_pnl']) for line in result),Decimal(0))
    fees=sum((Decimal(line['commissions_and_fees']) for line in result),Decimal(0))
    net=sum((Decimal(line['net_realized_pnl']) for line in result),Decimal(0))
    entry_time=min(entry_times) if entry_times else None;exit_time=max(exit_times) if exit_times else None
    if net!=gross-fees:raise ApplicationError('HISTORY_PORTFOLIO_TOTAL_DISAGREEMENT')
    return dict(per_strike=result,total_contracts=sum(line['entry_quantity'] for line in result),
        strike_count=sum(line['entry_quantity']>0 for line in result),gross_realized_pnl=number(gross),
        commissions_and_fees=number(fees),net_realized_pnl=number(net),entry_time=entry_time.isoformat() if entry_time else None,
        exit_time=exit_time.isoformat() if exit_time else None,
        duration_seconds=number(Decimal(str((exit_time-entry_time).total_seconds()))) if entry_time and exit_time else None,
        executions=histories,markers=markers,currency='USD',weighted_average_precision_digits=50,
        fee_basis='LATEST_REPORT_PER_ACTIVE_EXECUTION_REVISION')
