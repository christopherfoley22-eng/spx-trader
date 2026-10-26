"""Private, durable read-only broker callback evidence, NOT economic authority.

No broker import or order API. A history completion is not proof that history
covers all local orders. Candidate correction relationships require reconciliation.
"""
from dataclasses import asdict
import hashlib,json
from decimal import Decimal,InvalidOperation
from executor_broker_reality import BrokerRealityValidation
from executor_paper_broker import execution_family
from mortificatio_store import FoundationStore,encode
from simulation_evidence import EvidenceError


class BrokerLifecycleValidation(BrokerRealityValidation):
    def __init__(self,*args,capture_path,**kwargs):
        super().__init__(*args,**kwargs)
        self.capture=FoundationStore(capture_path,dict(component='READ_ONLY_BROKER_LIFECYCLE_V1',account=self.approval.account),lambda:self.wall().timestamp())
        self.capture.transact(lambda db,now:db.execute('CREATE TABLE IF NOT EXISTS broker_observations (identity TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,receipt_utc TEXT NOT NULL,receipt_monotonic REAL NOT NULL)'))
        self.history_requests=set()

    def record(self,kind,identity,payload):
        self.gate();raw=encode(payload)
        def write(db,now):
            old=db.execute('SELECT kind,payload FROM broker_observations WHERE identity=?',(identity,)).fetchone()
            if old:
                if tuple(old)==(kind,raw):return 'DUPLICATE'
                conflict=encode(dict(identity=identity,kind=kind,conflicting_payload=payload))
                conflict_id='conflict:'+hashlib.sha256(conflict.encode()).hexdigest()
                if not db.execute('SELECT 1 FROM broker_observations WHERE identity=?',(conflict_id,)).fetchone():
                    receipt=self.wall().isoformat();mono=self.mono()
                    db.execute('INSERT INTO broker_observations VALUES (?,?,?,?,?)',(conflict_id,'CONFLICT',conflict,receipt,mono))
                    self.capture.audit(db,'READ_ONLY_BROKER_OBSERVATION',identity=conflict_id,kind_name='CONFLICT',
                        row_digest=hashlib.sha256(encode([conflict_id,'CONFLICT',conflict,receipt,mono]).encode()).hexdigest())
                return 'CONFLICT'
            receipt=self.wall().isoformat();mono=self.mono()
            db.execute('INSERT INTO broker_observations VALUES (?,?,?,?,?)',(identity,kind,raw,receipt,mono))
            self.capture.audit(db,'READ_ONLY_BROKER_OBSERVATION',identity=identity,kind_name=kind,
                row_digest=hashlib.sha256(encode([identity,kind,raw,receipt,mono]).encode()).hexdigest())
            return 'RECORDED'
        result=self.capture.transact(write)
        if result=='CONFLICT':raise EvidenceError('CONFLICTING_BROKER_OBSERVATION')
        return result

    def order_status(self,order_id,status,filled,remaining,average,perm_id,parent_id,last_price,client_id,why_held,market_cap_price=0):
        # orderStatus has no account field. Never manufacture one from a socket.
        payload=dict(order_id=order_id,perm_id=perm_id,client_id=client_id,parent_id=parent_id,status=status,
            filled=str(filled),remaining=str(remaining),average_price=str(average),last_price=str(last_price),
            why_held=why_held,source_account=None,session_approved_account=self.approval.account,
            source_timestamp=None,economic_application_permitted=False)
        return self.record('ORDER_STATUS','status:'+hashlib.sha256(encode(payload).encode()).hexdigest(),payload)

    def detailed_order(self,*args):
        super().detailed_order(*args)
        row=self.order_details[args[0]]
        stable={k:v for k,v in row.items() if not k.startswith('receipt_')}
        return self.record('OPEN_ORDER','open:'+hashlib.sha256(encode(stable).encode()).hexdigest(),stable)

    def execution_details(self,request_id,contract,execution):
        self.gate()
        if execution.acctNumber!=self.approval.account:raise EvidenceError('EXECUTION_ACCOUNT_NOT_APPROVED')
        identity=execution.execId
        family,revision=execution_family(identity)
        if not isinstance(identity,str) or not identity:raise EvidenceError('EXECUTION_ID_REQUIRED')
        try:
            shares=Decimal(str(execution.shares));price=Decimal(str(execution.price))
            if not shares.is_finite() or shares<0 or shares!=shares.to_integral_value() or not price.is_finite() or price<0:raise ValueError()
        except (InvalidOperation,ValueError):raise EvidenceError('INVALID_EXECUTION_QUANTITY_OR_PRICE')
        # Store source execution time verbatim. No assumed timezone conversion.
        payload=dict(exec_id=identity,account=execution.acctNumber,order_ref=execution.orderRef,order_id=execution.orderId,
            perm_id=execution.permId,client_id=execution.clientId,side=execution.side,quantity=str(shares),price=str(price),
            con_id=contract.conId,symbol=contract.symbol,sec_type=contract.secType,right=contract.right,
            expiration=contract.lastTradeDateOrContractMonth,trading_class=contract.tradingClass,strike=str(contract.strike),
            multiplier=contract.multiplier,currency=contract.currency,source_execution_time_raw=execution.time,
            source_clock_domain='UNVERIFIED',pending_price_revision=getattr(execution,'pendingPriceRevision',None),
            family=family,revision=revision,economic_application_permitted=False)
        return self.record('EXECUTION','exec:'+identity,payload)

    def execution_end(self,request_id):
        if request_id not in self.history_requests:raise EvidenceError('UNREQUESTED_EXECUTION_HISTORY_END')
        return self.record('EXECUTION_END','end:'+str(self.generation)+':'+str(request_id),dict(request_id=request_id,available_response_complete=True,
            covers_all_local_intents=False,source_history_start=None,economic_application_permitted=False))

    def commission_report(self,report):
        payload=dict(exec_id=report.execId,amount=str(report.commission),currency=report.currency,economic_application_permitted=False)
        return self.record('COMMISSION','fee:'+hashlib.sha256(encode(payload).encode()).hexdigest(),payload)

    def error(self,request_id,code):
        if self.verified and not self.failure and not self.closed:
            payload=dict(request_or_order_id=request_id,code=code,source_account=None,economic_application_permitted=False)
            self.record('BROKER_ERROR','error:'+hashlib.sha256(encode(payload).encode()).hexdigest(),payload)
        return super().error(request_id,code)

    def report(self):
        result=super().report()
        def read(db,now):
            rows=[dict(r) for r in db.execute('SELECT * FROM broker_observations ORDER BY rowid')]
            audits=[json.loads(r[0]) for r in db.execute('SELECT record FROM security_audit ORDER BY sequence')]
            hashes={x['details']['identity']:x['details']['row_digest'] for x in audits if x['kind']=='READ_ONLY_BROKER_OBSERVATION'}
            if set(hashes)!={r['identity'] for r in rows}:raise EvidenceError('BROKER_CAPTURE_AUDIT_SCOPE_MISMATCH')
            for r in rows:
                values=[r[k] for k in ('identity','kind','payload','receipt_utc','receipt_monotonic')]
                if hashes[r['identity']]!=hashlib.sha256(encode(values).encode()).hexdigest():raise EvidenceError('BROKER_CAPTURE_INTEGRITY_FAILURE')
            return rows
        rows=self.capture.transact(read)
        # Do not bind revisions to economic orders here. An out-of-order or
        # incomplete family remains visible for the authoritative reconciler.
        executions=[json.loads(r['payload']) for r in rows if r['kind']=='EXECUTION'];families={}
        for x in executions:families.setdefault(x['family'],[]).append(x['exec_id'])
        result['lifecycle_capture']=dict(observations=rows,execution_families=families,
            unresolved_conflicts=sum(r['kind']=='CONFLICT' for r in rows),
            mode='PRIVATE_READ_ONLY_EVIDENCE_NOT_LEDGER_RECONCILIATION',covers_all_local_intents=False,
            order_mutation_enabled=False,broker_protection_verified=False)
        return result
