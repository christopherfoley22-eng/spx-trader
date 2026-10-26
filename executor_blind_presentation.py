"""Allowlisted dashboard DTOs. Internal protective-service state stays untouched."""


def subset(data,names):return {name:data[name] for name in names if name in data}


def allocation_view(data):
    result=subset(data,('direction','expiration','total_contracts','risk_policy_version','submission_enabled'))
    result['lines']=[dict(subset(line,('rank','label','quantity')),contract=subset(line.get('contract',{}),
        ('con_id','symbol','sec_type','right','expiration','trading_class','multiplier','currency','strike'))) for line in data.get('lines',[])]
    return result


def trade_view(data):
    result=subset(data,('trade_id','state','reconciled_flat','reservation_state','reconciliation_state','direction',
        'open_contracts','allocation','pending_orders','entry_blocked','emergency','protection_mode','submission_enabled'))
    # Allocation subobjects are allowlisted too; never pass arbitrary nesting.
    result['allocation']=[subset(line,('strike','target','owned')) for line in data.get('allocation',[])]
    result['premium_protection']=[subset(line,('contract_id','quantity','exit_quote_healthy','broker_held'))
                                  for line in data.get('premium_protection',[])]
    result['risk_control_health']='EMERGENCY' if data.get('emergency') else 'ATTENTION_REQUIRED' if data.get('recovery_reason') or data.get('state') in ('RECOVERY_REQUIRED','PENDING_RECONCILIATION') else 'HEALTHY'
    if data.get('native_protection_state')=='CRITICAL_UNRESOLVED_PROTECTION':result['risk_control_health']='CRITICAL_UNRESOLVED'
    condition=data.get('exit_execution_health')
    allowed={'WAIT_AUTHORIZED_EXIT','CONFIRMED_FLAT','AWAIT_FULL_RECONCILIATION','CRITICAL_RECONCILIATION_REQUIRED',
        'CRITICAL_EXIT_UNRESOLVED','PROTECTED_EXIT_COORDINATION_REQUIRED','EXIT_WORKING','EXIT_PACING_OR_LIQUIDITY_WAIT'}
    if condition is not None:
        result['exit_execution_health']=condition if condition in allowed else 'CRITICAL_EXECUTION_STATE_UNAVAILABLE'
        if result['exit_execution_health'].startswith('CRITICAL'):result['risk_control_health']='CRITICAL_UNRESOLVED'
        elif condition in {'PROTECTED_EXIT_COORDINATION_REQUIRED','EXIT_PACING_OR_LIQUIDITY_WAIT'}:result['risk_control_health']='ATTENTION_REQUIRED'
    return result


def lifecycle_view(data):
    result=subset(data,('mode','origin','real_ibkr_authenticated','broker_connected','account_reconciled','submission_enabled',
        'supervision_mode','supervisor_running','protective_condition','entry_fenced','foundation_available'))
    result['trades']=[trade_view(trade) for trade in data.get('trades',[])]
    if str(data.get('protective_condition','')).startswith('CRITICAL'):
        for trade in result['trades']:trade['risk_control_health']='CRITICAL_UNRESOLVED'
    elif data.get('entry_fenced'):
        for trade in result['trades']:
            if trade['risk_control_health']=='HEALTHY':trade['risk_control_health']='ATTENTION_REQUIRED'
    return result


def dashboard_response(path,data):
    if path=='/api/status':
        result=subset(data,('mode','entry_epoch','account_blocked','submission_enabled','real_broker_verified'))
        result['pending_commands']=[subset(row,('command_id','trade_id','direction','state')) for row in data.get('pending_commands',[])]
        result['emergency']=subset(data.get('emergency',{}),('latched','phase','executable'))
        if 'lifecycle' in data:result['lifecycle']=lifecycle_view(data['lifecycle'])
    elif path=='/api/commands' or path.startswith('/api/commands/'):
        result=subset(data,('result','trade_id','state','submission_enabled'))
        if 'allocation' in data:result['allocation']=allocation_view(data['allocation'])
        if 'lifecycle' in data:result['lifecycle']=trade_view(data['lifecycle'])
    elif path=='/api/exit-all':result=subset(data,('result','emergency','state','submission_enabled'))
    else:return data
    result['blind_execution']=True
    return result
