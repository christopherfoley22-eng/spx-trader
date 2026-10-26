"""Durable, authenticated, source-separated realized history. No trading actions.

Read requests rebuild a materialized view from verified durable journals. They
never call supervision, dispatch, cancel, or otherwise authorize execution.
"""
from datetime import date,datetime,timezone
from decimal import Decimal,localcontext
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from executor_paper_broker import canonical,utc
from executor_portfolio import OfflinePortfolioLedger
from executor_trade_accounting import closed_accounting,number
from mortificatio_models import ApplicationError
from mortificatio_store import encode
from simulation_evidence import MAX_AGE_SECONDS

ORIGIN='OFFLINE_FIXTURE'
PENDING='PENDING_RECONCILIATION'


def replay_journal(engine):
    """Read-only replay via existing reducers; do not open a second writer."""
    with engine.lock:return _replay_journal(engine)


def _replay_journal(engine):
    state=OfflinePortfolioLedger.__new__(OfflinePortfolioLedger)
    state.state=OfflinePortfolioLedger._empty();state.approval=engine.approval
    head='';sequence=0;events=[];exit_context={}
    for seq,event_id,kind,raw,digest in engine.db.execute('SELECT * FROM events ORDER BY sequence'):
        payload=json.loads(raw)
        expected=hashlib.sha256(canonical([head,seq,event_id,kind,raw]).encode()).hexdigest()
        if seq!=sequence+1 or digest!=expected or canonical(payload)!=raw:
            raise ApplicationError('HISTORY_JOURNAL_INTEGRITY_FAILURE')
        state._reduce(kind,payload);head=digest;sequence=seq;events.append((event_id,kind,payload))
        if kind=='PORT_EXECUTION' and payload['record']['side']=='SELL' and not payload['record']['bust']:
            exit_context.setdefault(payload['record']['exec_id'],dict(sequence=seq,reason=state.portfolio['exit_reason']))
    if (engine.db.execute('SELECT sequence,head FROM binding').fetchone()!=(sequence,head)
            or engine.head!=head or engine.sequence!=sequence or state.state!=engine.state):
        raise ApplicationError('HISTORY_JOURNAL_STATE_DISAGREEMENT')
    return state.state,events,dict(sequence=sequence,journal_head=head,broker_revision=state.state['revision'],positions=state.positions(),exit_context=exit_context)


def closing_exit_reasons(state,proof):
    """Historical reasons follow actual closing fills, not future account latches.

    A price correction belongs to its original execution family and must not
    turn a later emergency into the original economic execution's exit cause.
    """
    strikes={};last=None
    for identity,item in state['executions'].items():
        record=item['record']
        if not item['active'] or record['bust'] or record['side']!='SELL':continue
        root=identity;seen=set()
        while state['executions'][root]['record']['correction_of']:
            if root in seen:raise ApplicationError('HISTORY_EXECUTION_CORRECTION_CYCLE')
            seen.add(root);root=state['executions'][root]['record']['correction_of']
        context=proof['exit_context'].get(root)
        if context is None or not context['reason']:raise ApplicationError('HISTORY_EXECUTION_EXIT_CAUSE_REQUIRED')
        cid=str(record['contract']['con_id'])
        if cid not in strikes or context['sequence']>strikes[cid]['sequence']:strikes[cid]=context
        if last is None or context['sequence']>last['sequence']:last=context
    return {cid:value['reason'] for cid,value in strikes.items()},last['reason'] if last else None


def chart_evidence(events,accounting):
    observations={};ambiguous=False
    def add(source_time,price,event_id):
        nonlocal ambiguous
        if source_time is None or price is None:return
        stamp=utc(datetime.fromisoformat(source_time)).isoformat()
        if accounting['entry_time'] and stamp<accounting['entry_time'] or accounting['exit_time'] and stamp>accounting['exit_time']:return
        value=str(Decimal(str(price)))
        if stamp in observations and Decimal(observations[stamp]['price'])!=Decimal(value):ambiguous=True
        observations.setdefault(stamp,dict(source_time=stamp,price=value,source=ORIGIN,evidence_ids=[]))['evidence_ids'].append(event_id)
    for event_id,kind,payload in events:
        if kind=='PORT_SPX' and payload['status']=='LIVE':add(payload['source'],payload['price'],event_id)
        elif kind=='PORT_QUOTE':
            spx=payload['quote'].get('spx')
            if spx and spx['status']=='LIVE':add(spx['source_time'],spx['price'],event_id)
        elif kind=='PORT_CONFIG' and payload.get('allocation'):
            for quote in payload['allocation']['market_evidence']['quotes']:
                spx=quote.get('spx')
                if spx and spx['status']=='LIVE':add(spx['source_time'],spx['price'],event_id+':'+quote['quote_id'])
    points=sorted(observations.values(),key=lambda p:p['source_time'])
    available=len(points)>=2 and not ambiguous
    markers=[]
    for original in accounting['markers']:
        marker=dict(original)
        if not ambiguous and marker['source_time'] in observations:marker['spx_price']=observations[marker['source_time']]['price']
        markers.append(marker)
    return dict(status='AVAILABLE' if available else 'UNAVAILABLE',
        reason=None if available else 'AMBIGUOUS_SOURCE_TIMESTAMP' if ambiguous else 'INSUFFICIENT_RECORDED_SPX_HISTORY',
        source=ORIGIN,series_type='RECORDED_SPX_OBSERVATIONS',observations=points,execution_markers=markers,
        candles=None,candle_status='UNAVAILABLE_NO_RECORDED_CANDLES',interpolation_performed=False)


class TradeHistoryService:
    def __init__(self,lifecycle):
        self.lifecycle=lifecycle;self.security=lifecycle.security
        self.last_error=None
        def schema(db,now):
            db.execute("CREATE TABLE IF NOT EXISTS trade_history (trade_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,account TEXT NOT NULL,environment TEXT NOT NULL,source TEXT NOT NULL CHECK(source='OFFLINE_FIXTURE'),revision INTEGER NOT NULL,payload TEXT NOT NULL,report_digest TEXT NOT NULL,chain_head TEXT NOT NULL)")
            db.execute('CREATE TABLE IF NOT EXISTS trade_history_revisions (trade_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,report_digest TEXT NOT NULL,previous_head TEXT NOT NULL,chain_head TEXT NOT NULL,PRIMARY KEY(trade_id,revision))')
        self.security.store.transact(schema)

    def _fresh(self):
        owner=self.lifecycle;now=utc(owner.wall_clock());mono=owner.monotonic_clock();session=owner.anchor.session
        if owner.closed or not owner.fixture.connected or owner.proof is None or session is None:return False
        if not utc(session.verified_at)<=now<min(utc(session.expires_at),utc(owner.approval.approved_until)):return False
        if owner.proof.generation!=owner.anchor.state['generation']:return False
        for engine in owner.engines.values():
            s=engine.session
            if (s is None or engine.state['faults'] or engine.reconciled_at is None or not 0<=mono-engine.reconciled_at<=MAX_AGE_SECONDS
                    or engine.state['revision']!=engine.state['reconciled_revision']
                    or not utc(s.verified_at)<=now<min(utc(s.expires_at),utc(engine.approval.approved_until))):return False
        for _,receipt in owner.proof.component_receipts:
            if type(receipt) not in (int,float) or not math.isfinite(receipt) or not 0<=mono-receipt<=MAX_AGE_SECONDS:return False
        return True

    def _validate_projection(self,db,trade):
        head='';revision=0;last=None
        for row in db.execute('SELECT revision,payload,report_digest,previous_head,chain_head FROM trade_history_revisions WHERE trade_id=? ORDER BY revision',(trade,)):
            seq,raw,digest,previous,record_head=tuple(row)
            expected_digest=hashlib.sha256(raw.encode()).hexdigest()
            expected_head=hashlib.sha256(encode([head,seq,digest]).encode()).hexdigest()
            if seq!=revision+1 or digest!=expected_digest or previous!=head or record_head!=expected_head or encode(json.loads(raw))!=raw:
                raise ApplicationError('HISTORY_STORE_INTEGRITY_FAILURE',503)
            revision=seq;head=record_head;last=raw
        row=db.execute('SELECT revision,payload,report_digest,chain_head FROM trade_history WHERE trade_id=?',(trade,)).fetchone()
        if (row is None)!=(revision==0) or row is not None and tuple(row)!=(revision,last,hashlib.sha256(last.encode()).hexdigest(),head):
            raise ApplicationError('HISTORY_PROJECTION_INTEGRITY_FAILURE',503)
        return json.loads(last) if last else None,revision,head

    def refresh_locked(self):
        """Called under the lifecycle lock; no broker/risk state mutations."""
        owner=self.lifecycle
        def commands(db,now):return [dict(row) for row in db.execute('SELECT * FROM commands')]
        rows=self.security.store.transact(commands)
        with self.security.store.database() as db:
            cursor=db.execute('SELECT callback_cursor FROM execution_service').fetchone()[0]
        pending_callbacks=bool(owner.fixture.callbacks_after(cursor))
        fresh=self._fresh() and not pending_callbacks
        for command in rows:
            trade=command['trade_id'];engine=owner.engines.get(trade)
            created=datetime.fromtimestamp(command['created'],timezone.utc)
            report=dict(trade_id=trade,status=PENDING,data_origin=ORIGIN,synthetic=True,direction=command['direction'],
                date=created.astimezone(ZoneInfo('America/New_York')).date().isoformat(),date_basis='ADMISSION_TIME',
                admission_time=created.isoformat(),total_contracts=None,strike_count=None,per_strike=[],totals=None,
                entry_time=None,exit_time=None,duration_seconds=None,exit_reason=None,reconciliation_status=PENDING,
                chart=dict(status='WITHHELD_BLIND_EXECUTION'),blind_execution=True,pending_reasons=[])
            proof=None
            try:
                if engine is None:raise ApplicationError('HISTORY_MANAGED_JOURNAL_UNAVAILABLE')
                state,events,proof=replay_journal(engine);p=state['portfolio']
                if not p.get('allocation') or p['allocation']['binding']!=dict(account=command['account'],environment=command['environment'],risk_version=command['risk_version']):
                    raise ApplicationError('HISTORY_ALLOCATION_BINDING_INVALID')
                report['per_strike']=[dict(contract=target['contract'],strike=target['contract']['strike'],
                    entry_quantity=sum(e['record']['quantity'] for e in state['executions'].values() if e['active'] and not e['record']['bust'] and e['record']['side']=='BUY' and str(e['record']['contract']['con_id'])==cid),
                    owned_quantity=proof['positions'].get(int(cid),0)) for cid,target in p['targets'].items()]
                report['total_contracts']=sum(line['entry_quantity'] for line in report['per_strike'])
                report['strike_count']=sum(line['entry_quantity']>0 for line in report['per_strike'])
                with self.security.store.database() as db:
                    reservation=db.execute('SELECT state FROM allocation_reservations WHERE trade_id=?',(trade,)).fetchone()
                    registry=db.execute('SELECT state,fingerprint FROM execution_lifecycle WHERE trade_id=?',(trade,)).fetchone()
                    undelivered=db.execute('SELECT 1 FROM execution_publications WHERE trade_id=? AND delivered=0',(trade,)).fetchone()
                reasons=report['pending_reasons']
                if not fresh:reasons.append('BROKER_RECONCILIATION_UNAVAILABLE')
                if pending_callbacks:reasons.append('UNPROCESSED_BROKER_EVIDENCE')
                if proof['positions']:reasons.append('MANAGED_POSITION_OPEN')
                if engine.state['revision']!=state['revision']:reasons.append('BROKER_EVIDENCE_CHANGED_DURING_READ')
                if any(not i['terminal_proven'] or i['uncertain'] for i in state['intents'].values()):reasons.append('ORDER_RECONCILIATION_INCOMPLETE')
                if state['faults']:reasons.append('BROKER_EVIDENCE_QUARANTINED')
                if not reservation or reservation[0]!='RELEASED':reasons.append('CAPITAL_RESERVATION_HELD')
                if (command['state'] not in ('COMPLETED','REJECTED_CONFIRMED') or not registry or registry[0]!='FLAT_CONFIRMED' or undelivered):
                    reasons.append('LIFECYCLE_TERMINAL_PROOF_REQUIRED')
                if not p['halted'] and not p['exit_reason']:reasons.append('ACQUISITION_NOT_SEALED')
                fingerprint=json.loads(registry[1]) if registry and registry[1] else {}
                if (fingerprint.get('reconciliation_complete') is not True or fingerprint.get('execution_history_complete') is not True
                        or fingerprint.get('commissions_complete') is not True or fingerprint.get('position_quantity')!=0
                        or fingerprint.get('outstanding_orders')!=0 or fingerprint.get('generation')!=owner.anchor.state['generation']):
                    reasons.append('TERMINAL_RECONCILIATION_EVIDENCE_REQUIRED')
                if not reasons:
                    strike_reasons,exit_reason=closing_exit_reasons(state,proof)
                    # This is the read-only replay copy, never the risk engine.
                    p['exit_reason']=exit_reason
                    accounting=closed_accounting(state)
                    for line in accounting['per_strike']:
                        line['exit_reason']=strike_reasons.get(str(line['contract']['con_id']))
                    report.update(status='FINALIZED' if accounting['total_contracts'] else 'CANCELLED_NO_FILL',blind_execution=False,
                        date=(datetime.fromisoformat(accounting['entry_time']).astimezone(ZoneInfo('America/New_York')).date().isoformat()
                              if accounting['entry_time'] else report['date']),
                        date_basis='ENTRY_SOURCE_TIME' if accounting['entry_time'] else 'ADMISSION_TIME',
                        total_contracts=accounting['total_contracts'],strike_count=accounting['strike_count'],
                        per_strike=accounting['per_strike'],totals={key:accounting[key] for key in ('gross_realized_pnl','commissions_and_fees','net_realized_pnl','currency')},
                        entry_time=accounting['entry_time'],exit_time=accounting['exit_time'],duration_seconds=accounting['duration_seconds'],
                        exit_reason=p['exit_reason'],reconciliation_status='RECONCILED',chart=chart_evidence(events,accounting),
                        execution_history=accounting['executions'],
                        commission_history=[payload['record'] for _,kind,payload in events if kind=='PORT_COMMISSION'],
                        order_history=[payload['record'] for _,kind,payload in events if kind=='PORT_ORDER'],
                        fee_basis=accounting['fee_basis'],weighted_average_precision_digits=accounting['weighted_average_precision_digits'])
                    if report['status']=='CANCELLED_NO_FILL':
                        report.update(blind_execution=True,totals=None,per_strike=[dict(contract=t['contract'],strike=t['strike'],entry_quantity=0,owned_quantity=0) for t in report['per_strike']],
                            chart=dict(status='UNAVAILABLE',reason='NO_EXECUTED_TRADE'),execution_history=[],commission_history=[],order_history=[])
            except Exception:
                report.update(status=PENDING,blind_execution=True,totals=None,entry_time=None,exit_time=None,duration_seconds=None,exit_reason=None,
                    reconciliation_status=PENDING,chart=dict(status='WITHHELD_BLIND_EXECUTION'))
                report['pending_reasons'].append('ACCOUNTING_OR_JOURNAL_EVIDENCE_UNAVAILABLE')
                report.pop('execution_history',None);report.pop('commission_history',None);report.pop('order_history',None)
            self._persist(command,report,proof)
        self.last_error=None

    def _persist(self,command,report,proof):
        trade=command['trade_id']
        def save(db,now):
            previous,revision,head=self._validate_projection(db,trade)
            # Verification refreshes alone do not fabricate new economic history.
            comparable=dict(previous or {})
            for key in ('revision','finalized_at','prior_finalization_revoked','as_of_utc'):comparable.pop(key,None)
            if comparable==report:return
            new=dict(report,revision=revision+1,as_of_utc=datetime.fromtimestamp(now,timezone.utc).isoformat(),
                prior_finalization_revoked=bool(previous and (previous['status']=='FINALIZED' or previous.get('prior_finalization_revoked'))))
            new['finalized_at']=new['as_of_utc'] if new['status']=='FINALIZED' else None
            raw=encode(new);digest=hashlib.sha256(raw.encode()).hexdigest()
            chain=hashlib.sha256(encode([head,revision+1,digest]).encode()).hexdigest()
            db.execute('INSERT INTO trade_history_revisions VALUES (?,?,?,?,?,?)',(trade,revision+1,raw,digest,head,chain))
            db.execute('INSERT OR REPLACE INTO trade_history VALUES (?,?,?,?,?,?,?,?,?)',
                (trade,command['user_id'],command['account'],command['environment'],ORIGIN,revision+1,raw,digest,chain))
            self.security.store.audit(db,'TRADE_HISTORY_CHANGED',trade_id=trade,revision=revision+1,report_digest=digest,state=new['status'])
        self.security.store.transact(save)

    def _authorized(self,session,rate=False):
        self.lifecycle._ensure_open()
        def authorize(db,now):
            user=self.security.require_session(db,now,session)['user_id']
            if rate:
                scopes=(('history-user:'+user,self.security.policy.requests_per_minute),
                        ('history-account:'+self.security.application_policy.binding.account,self.security.policy.requests_per_minute*4))
                for bucket,limit in scopes:
                    if db.execute('SELECT count(*) FROM rate_events WHERE bucket=? AND stamp>?',(bucket,now-60)).fetchone()[0]>=limit:
                        raise ApplicationError('HISTORY_RATE_LIMITED',429)
                for bucket,_ in scopes:db.execute('INSERT INTO rate_events VALUES (?,?)',(bucket,now))
                db.execute('DELETE FROM rate_events WHERE stamp<=?',(now-60,))
            return user
        return self.security.store.transact(authorize)

    def _rows(self,session):
        self._authorized(session)
        with self.lifecycle.lock:
            try:self.refresh_locked()
            except Exception:
                self.last_error='HISTORY_REPORTING_UNAVAILABLE';raise
            def read(db,now):
                user=self.security.require_session(db,now,session)['user_id'];binding=self.security.application_policy.binding
                rows=[]
                for row in db.execute('SELECT h.trade_id FROM trade_history h JOIN commands c ON c.trade_id=h.trade_id WHERE c.user_id=? AND c.account=? AND c.environment=? AND h.user_id=c.user_id AND h.account=c.account AND h.environment=c.environment AND h.source=?',
                        (user,binding.account,binding.environment,ORIGIN)):
                    rows.append(self._validate_projection(db,row[0])[0])
                return rows
            return self.security.store.transact(read)

    @staticmethod
    def filters(parameters):
        parameters=dict(parameters or {})
        if set(parameters)-{'state','direction','start_date','end_date','search','page','page_size','source'}:
            raise ApplicationError('HISTORY_FILTER_NOT_ALLOWED',400)
        if parameters.get('source',ORIGIN)!=ORIGIN:raise ApplicationError('HISTORY_SOURCE_UNAVAILABLE',400)
        state=parameters.get('state','FINALIZED')
        if state not in ('FINALIZED',PENDING,'CANCELLED_NO_FILL','ALL'):raise ApplicationError('INVALID_HISTORY_STATE',400)
        direction=parameters.get('direction')
        if direction not in (None,'CALL','PUT'):raise ApplicationError('INVALID_HISTORY_DIRECTION',400)
        start,end=parameters.get('start_date'),parameters.get('end_date')
        try:
            for value in (start,end):
                if value is not None and (not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value):raise ValueError
            if start and end and start>end:raise ValueError
            page=parameters.get('page','1');size=parameters.get('page_size','25')
            if isinstance(page,bool) or isinstance(size,bool) or str(int(page))!=str(page) or str(int(size))!=str(size):raise ValueError
            page,size=int(page),int(size)
            if not 1<=page<=1000000 or not 1<=size<=100:raise ValueError
        except (TypeError,ValueError):raise ApplicationError('INVALID_HISTORY_DATE_OR_PAGINATION',400)
        search=parameters.get('search','')
        if not isinstance(search,str) or len(search)>120 or any(ord(c)<32 for c in search):raise ApplicationError('INVALID_HISTORY_SEARCH',400)
        return dict(state=state,direction=direction,start_date=start,end_date=end,search=search.casefold(),page=page,page_size=size)

    def _select(self,rows,filters):
        result=[]
        for row in rows:
            if filters['state']!='ALL' and row['status']!=filters['state']:continue
            if filters['direction'] and row['direction']!=filters['direction']:continue
            if filters['start_date'] and row['date']<filters['start_date'] or filters['end_date'] and row['date']>filters['end_date']:continue
            text=encode(dict(trade_id=row['trade_id'],direction=row['direction'],date=row['date'],
                strikes=[line['strike'] for line in row['per_strike']],exit_reason=row['exit_reason'])).casefold()
            if filters['search'] and filters['search'] not in text:continue
            result.append(row)
        return sorted(result,key=lambda r:(r['date'],r['admission_time'],r['trade_id']),reverse=True)

    def list_trades(self,session,parameters=None):
        self._authorized(session,rate=True);filters=self.filters(parameters);rows=self._select(self._rows(session),filters)
        start=(filters['page']-1)*filters['page_size']
        fields=('trade_id','status','revision','data_origin','synthetic','direction','date','date_basis','total_contracts','strike_count',
            'totals','duration_seconds','exit_reason','reconciliation_status','pending_reasons','blind_execution','prior_finalization_revoked',
            'entry_time','admission_time')
        return dict(data_origin=ORIGIN,synthetic=True,items=[{k:row[k] for k in fields} for row in rows[start:start+filters['page_size']]],
            total_records=len(rows),page=filters['page'],page_size=filters['page_size'],submission_enabled=False)

    def detail(self,session,trade_id):
        self._authorized(session,rate=True)
        rows=self._rows(session)
        row=next((r for r in rows if r['trade_id']==trade_id),None)
        if row is None:raise ApplicationError('HISTORY_TRADE_NOT_FOUND',404)
        return row

    def chart(self,session,trade_id):return dict(trade_id=trade_id,data_origin=ORIGIN,synthetic=True,**self.detail(session,trade_id)['chart'])

    def summary(self,session,parameters=None):
        self._authorized(session,rate=True);filters=self.filters(parameters)
        filters['state']='FINALIZED';rows=self._select(self._rows(session),filters)
        with localcontext() as context:
            context.prec=50
            amounts=[Decimal(row['totals']['net_realized_pnl']) for row in rows]
            count=len(amounts);total=sum(amounts,Decimal(0));wins=sum(value>0 for value in amounts);losses=sum(value<0 for value in amounts)
            return dict(data_origin=ORIGIN,synthetic=True,total_completed_trades=count,wins=wins,losses=losses,breakevens=count-wins-losses,
                win_rate_percent=number(Decimal(wins)*100/count) if count else None,total_net_realized_pnl=number(total),
                average_net_realized_pnl=number(total/count) if count else None,currency='USD',submission_enabled=False)
