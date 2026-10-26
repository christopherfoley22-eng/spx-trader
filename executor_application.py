"""Unified Phase 1 application boundary. Durable OFFLINE commands, no execution.

An optional offline allocation boundary persists a funded plan with each command.
No order dispatch or network transport is added.
A broker evidence provider is trusted backend code, never an HTTP payload. The
production/default provider denies entry; test providers are explicitly fixtures.
"""
from dataclasses import asdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
import uuid

from mortificatio_models import (ApplicationError, TradeCommand, BrokerAdmissionEvidence, ManagedTradeEvidence)
from mortificatio_store import encode


class DisabledBrokerBoundary:
    def admission_evidence(self):raise ApplicationError('BROKER_ENTRY_EVIDENCE_UNAVAILABLE')


class UnifiedApplication:
    def __init__(self,security,protection_authority,broker_boundary=None,allocation_boundary=None):
        if (protection_authority.binding.account,protection_authority.binding.environment)!=(security.application_policy.binding.account,security.application_policy.binding.environment):raise ApplicationError('APPLICATION_PROTECTION_BINDING_MISMATCH')
        self.security,self.protection=security,protection_authority
        self.broker=broker_boundary or DisabledBrokerBoundary()
        self.policy=security.application_policy
        self.allocation=allocation_boundary

    @staticmethod
    def _utc(value):
        if not isinstance(value,datetime) or value.tzinfo is None:raise ApplicationError('EXPLICIT_EVIDENCE_TIME_REQUIRED')
        return value.astimezone(timezone.utc)

    def _broker(self,now,preflight=False):
        evidence=self.broker.admission_evidence()
        if (not isinstance(evidence,BrokerAdmissionEvidence) or evidence.origin!='OFFLINE_FIXTURE'
                or evidence.binding!=self.policy.binding or type(evidence.generation) is not int or evidence.generation<1
                or evidence.connected is not True or evidence.account_verified is not True
                or not isinstance(evidence.evidence_ref,str) or not evidence.evidence_ref
                or not 0<=now-self._utc(evidence.observed_at).timestamp()<=1
                or self._utc(evidence.expires_at).timestamp()<=now):
            raise ApplicationError('VERIFIED_OFFLINE_BROKER_BINDING_REQUIRED')
        if preflight and any(value is not True for value in (evidence.risk_passed,evidence.market_passed,evidence.buying_power_passed)):
            raise ApplicationError('RISK_MARKET_OR_BUYING_POWER_PREFLIGHT_FAILED')
        return evidence

    def begin_trading_authorization(self,session,csrf,peer):
        self.security.session_status(session)
        broker=self._broker(self.security.store.clock())
        return self.security.begin_step_up(session,csrf,peer,'TRADING_AUTHORIZE',broker.generation)

    def finish_trading_authorization(self,session,csrf,flow,response,peer):
        broker=self._broker(self.security.store.clock())
        return self.security.complete_step_up(session,csrf,flow,response,peer,'TRADING_AUTHORIZE',broker.generation)

    def dashboard_session(self,session):
        self.security.session_status(session)
        generation=None
        try:generation=self._broker(self.security.store.clock(),preflight=True).generation
        except Exception:pass
        result=self.security.dashboard_session(session,generation)
        result['broker_evidence_available']=generation is not None
        result['broker_evidence_origin']='OFFLINE_FIXTURE' if generation is not None else 'UNAVAILABLE'
        result['real_ibkr_authenticated']=False
        result['limits']={'per_strike':25,'portfolio':125,'trades_per_day':self.policy.max_trades_per_day}
        return result

    def _account_row(self,db):
        binding=self.policy.binding
        db.execute('INSERT OR IGNORE INTO account_state VALUES (?,?,0,0)',(binding.account,binding.environment))
        return db.execute('SELECT * FROM account_state WHERE account=? AND environment=?',(binding.account,binding.environment)).fetchone()

    def status(self,session):
        def operation(db,now):
            user=self.security.require_session(db,now,session)
            account=self._account_row(db)
            pending=db.execute("SELECT command_id,trade_id,direction,state FROM commands WHERE account=? AND environment=? AND user_id=? AND state NOT IN ('COMPLETED','REJECTED_CONFIRMED') ORDER BY created",
                (self.policy.binding.account,self.policy.binding.environment,user['user_id'])).fetchall()
            return {'mode':'OFFLINE_FOUNDATION_NO_EXECUTION','entry_epoch':account['entry_epoch'],
                'pending_commands':[dict(row) for row in pending],'account_blocked':bool(account['blocked']),
                'emergency':self.protection.emergency.status(),'submission_enabled':False,'real_broker_verified':False}
        return self.security.store.transact(operation)

    def direction_command(self,session,csrf,command):
        if not isinstance(command,TradeCommand):raise ApplicationError('DIRECTION_ONLY_COMMAND_REQUIRED',400)
        command.validate()
        def operation(db,now):
            user=self.security.require_session(db,now,session,csrf,write=True)
            old=db.execute('SELECT * FROM commands WHERE command_id=?',(command.command_id,)).fetchone()
            if old:
                if (old['user_id'],old['account'],old['environment'],old['direction'],old['entry_epoch'])!=(user['user_id'],self.policy.binding.account,self.policy.binding.environment,command.direction,command.entry_epoch):
                    raise ApplicationError('COMMAND_ID_REUSED_WITH_DIFFERENT_INTENT',409)
                # This is retrieval of an existing durable decision, never a retry
                # of order submission, even if the grant/broker has since expired.
                result={'result':'DUPLICATE','trade_id':old['trade_id'],'state':old['state'],'submission_enabled':False}
                if self.allocation is not None:result['allocation']=self.allocation.read(db,old['trade_id']).report()
                return result
            if self.protection.emergency.blocked():raise ApplicationError('EMERGENCY_BLOCKS_NEW_COMMANDS')
            broker=self._broker(now,preflight=True)
            grant=self.security.require_grant(db,now,user,broker.generation)
            account=self._account_row(db)
            if account['blocked']:raise ApplicationError('ACCOUNT_RECOVERY_REQUIRED')
            if command.entry_epoch!=account['entry_epoch']:raise ApplicationError('STALE_ENTRY_EPOCH',409)
            active=db.execute("SELECT 1 FROM commands WHERE account=? AND environment=? AND state NOT IN ('COMPLETED','REJECTED_CONFIRMED')",(self.policy.binding.account,self.policy.binding.environment)).fetchone()
            if active:raise ApplicationError('ACCOUNT_TRADE_ALREADY_ACTIVE',409)
            day=datetime.fromtimestamp(now,timezone.utc).astimezone(ZoneInfo('America/New_York')).date().isoformat()
            used=db.execute("SELECT count(*) FROM commands WHERE account=? AND environment=? AND ((ever_filled=1 AND fill_day=?) OR (ever_filled=0 AND admission_day=? AND state NOT IN ('COMPLETED','REJECTED_CONFIRMED')))",
                (self.policy.binding.account,self.policy.binding.environment,day,day)).fetchone()[0]
            if used>=self.policy.max_trades_per_day:raise ApplicationError('ACCOUNT_TRADE_FREQUENCY_LIMIT')
            trade=str(uuid.uuid4())
            allocation=self.allocation.prepare(self,db,now,command,trade,broker) if self.allocation is not None else None
            db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,0,?)",
                (command.command_id,trade,user['user_id'],self.policy.binding.account,self.policy.binding.environment,
                 command.direction,command.entry_epoch,grant['grant_id'],broker.generation,self.policy.binding.risk_version,
                 now,day,'QUEUED_OFFLINE',broker.generation))
            db.execute('UPDATE account_state SET entry_epoch=entry_epoch+1 WHERE account=? AND environment=?',(self.policy.binding.account,self.policy.binding.environment))
            self.security.store.audit(db,'DIRECTION_COMMAND_RESERVED',command_id=command.command_id,trade_id=trade)
            result={'result':'ACCEPTED_OFFLINE','trade_id':trade,'state':'QUEUED_OFFLINE','submission_enabled':False}
            if allocation is not None:result['allocation']=allocation.report()
            return result
        return self.security.store.transact(operation)

    def command_status(self,session,command_id):
        """Read-only recovery lookup; never re-admit or dispatch a command."""
        TradeCommand(command_id,'CALL',0).validate()
        def read(db,now):
            user=self.security.require_session(db,now,session)
            row=db.execute('SELECT * FROM commands WHERE command_id=? AND user_id=? AND account=? AND environment=?',
                (command_id,user['user_id'],self.policy.binding.account,self.policy.binding.environment)).fetchone()
            if row is None:raise ApplicationError('COMMAND_NOT_FOUND',404)
            result={'result':'RECORDED','trade_id':row['trade_id'],'state':row['state'],'submission_enabled':False}
            if self.allocation is not None:result['allocation']=self.allocation.read(db,row['trade_id']).report()
            return result
        return self.security.store.transact(read)

    def emergency_exit_all(self,session,csrf,request_id):
        TradeCommand(request_id,'CALL',0).validate()
        def authenticate(db,now):
            row=self.security.require_session(db,now,session,csrf,write=True)
            # Fence command admission in the same transaction as authorization.
            # If the independent latch write then fails, entry stays blocked.
            self._account_row(db)
            db.execute('UPDATE account_state SET blocked=1 WHERE account=? AND environment=?',
                       (self.policy.binding.account,self.policy.binding.environment))
            self.security.store.audit(db,'EMERGENCY_REQUEST_AUTHORIZED',user_id=row['user_id'],request_id=request_id)
        self.security.store.transact(authenticate)
        # No trading grant, buying power, broker readiness, or market eligibility
        # is required to persist emergency intent. Engine reconciliation still is.
        result=self.protection.emergency.request(request_id)
        return {'result':result,'emergency':True,'state':'PENDING_RECONCILIATION','submission_enabled':False}

    def observe_managed_trade(self,service_token,evidence):
        self.protection.require(service_token,'RECONCILE')
        if not isinstance(evidence,ManagedTradeEvidence) or evidence.origin!='OFFLINE_FIXTURE' or evidence.binding!=self.policy.binding:
            raise ApplicationError('TRUSTED_MANAGED_TRADE_EVIDENCE_REQUIRED')
        raw=asdict(evidence)
        raw['first_fill_at']=self._utc(evidence.first_fill_at).isoformat() if evidence.first_fill_at else None
        payload=encode(raw)
        def operation(db,now):
            previous=db.execute('SELECT * FROM lifecycle_events WHERE evidence_id=?',(evidence.evidence_id,)).fetchone()
            if previous:
                if previous['payload']!=payload:raise ApplicationError('CONFLICTING_LIFECYCLE_EVENT',409)
                return {'result':'DUPLICATE','submission_enabled':False}
            row=db.execute('SELECT * FROM commands WHERE trade_id=?',(evidence.trade_id,)).fetchone()
            if (row is None or evidence.binding.account!=row['account'] or evidence.binding.environment!=row['environment']
                    or type(evidence.generation) is not int or evidence.generation<row['last_generation']
                    or type(evidence.revision) is not int or evidence.revision<=row['last_revision']
                    or any(type(v) is not bool for v in (evidence.entry_filled,evidence.reconciliation_complete,evidence.execution_history_complete,evidence.commissions_complete))
                    or type(evidence.position_quantity) is not int or not 0<=evidence.position_quantity<=125
                    or type(evidence.outstanding_orders) is not int or evidence.outstanding_orders<0
                    or not isinstance(evidence.evidence_id,str) or not 1<=len(evidence.evidence_id)<=128):
                raise ApplicationError('INVALID_OR_STALE_MANAGED_TRADE_EVIDENCE')
            ever=bool(row['ever_filled']) or evidence.entry_filled
            fill_day,fill_at=row['fill_day'],row['first_fill_at']
            if evidence.entry_filled and not row['ever_filled']:
                if evidence.first_fill_at is None:raise ApplicationError('BROKER_FILL_TIME_REQUIRED')
                stamp=self._utc(evidence.first_fill_at)
                if not row['created']-1<=stamp.timestamp()<=now+.25:raise ApplicationError('INCONSISTENT_BROKER_FILL_TIME')
                fill_day=stamp.astimezone(ZoneInfo('America/New_York')).date().isoformat();fill_at=stamp.isoformat()
            if evidence.position_quantity and not ever:raise ApplicationError('POSITION_WITHOUT_ENTRY_EVIDENCE')
            flat=(evidence.position_quantity==0 and evidence.outstanding_orders==0 and evidence.reconciliation_complete
                  and evidence.execution_history_complete and evidence.commissions_complete)
            state='COMPLETED' if flat and ever else 'REJECTED_CONFIRMED' if flat else 'OPEN_OFFLINE' if evidence.position_quantity else 'PENDING_RECONCILIATION'
            if row['state'] in ('COMPLETED','REJECTED_CONFIRMED') and not flat:
                db.execute('UPDATE account_state SET blocked=1 WHERE account=? AND environment=?',(row['account'],row['environment']))
                self.security.store.audit(db,'LATE_EXPOSURE',trade_id=row['trade_id'])
                state='RECOVERY_REQUIRED'
            db.execute('UPDATE commands SET state=?,ever_filled=?,fill_day=?,first_fill_at=?,last_revision=?,last_generation=? WHERE trade_id=?',
                (state,int(ever),fill_day,fill_at,evidence.revision,evidence.generation,evidence.trade_id))
            db.execute('INSERT INTO lifecycle_events VALUES (?,?,?)',(evidence.evidence_id,evidence.trade_id,payload))
            self.security.store.audit(db,'MANAGED_TRADE_EVIDENCE_RECORDED',trade_id=evidence.trade_id,state=state)
            return {'result':'RECORDED','state':state,'submission_enabled':False}
        return self.security.store.transact(operation)
