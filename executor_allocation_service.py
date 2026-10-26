"""Durable Phase 2 admission and OFFLINE portfolio scheduler bridge.

No IBKR client, socket, real dispatch, or execution permit. Central reservations
are held unchanged through partial fills and cancellations until reconciled flat.
"""
from dataclasses import asdict,replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import uuid

from executor_allocation import AllocationPolicy, AllocationPlan, AllocationEvidence, build_allocation, amount, financial_room
from executor_portfolio_liquidity import fixture_from_payload, fixture_payload, assess_liquidity
from executor_paper_types import PaperBrokerError
from simulation_evidence import VerifiedOptionContract, EvidenceError
from mortificatio_models import ApplicationError
from mortificatio_store import encode


class AllocationBoundary:
    def __init__(self,policy,provider):
        if not isinstance(policy,AllocationPolicy):raise ApplicationError('EXPLICIT_ALLOCATION_POLICY_REQUIRED')
        self.policy=policy;self.configuration=encode(policy.normalized());self.provider=provider
        self.readiness=None

    def _schema(self,db,app):
        db.execute('CREATE TABLE IF NOT EXISTS allocation_policy (account TEXT NOT NULL,environment TEXT NOT NULL,risk_version TEXT NOT NULL,configuration TEXT NOT NULL,PRIMARY KEY(account,environment))')
        db.execute('CREATE TABLE IF NOT EXISTS allocation_plans (trade_id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS allocation_reservations (trade_id TEXT PRIMARY KEY,capital TEXT NOT NULL,stop_risk TEXT NOT NULL,delta_notional TEXT NOT NULL,state TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS allocation_bindings (trade_id TEXT PRIMARY KEY,journal_path TEXT UNIQUE NOT NULL,journal_id TEXT UNIQUE NOT NULL)')
        binding=app.policy.binding
        old=db.execute('SELECT risk_version,configuration FROM allocation_policy WHERE account=? AND environment=?',(binding.account,binding.environment)).fetchone()
        if old is not None and tuple(old)!=(binding.risk_version,self.configuration):raise ApplicationError('ALLOCATION_POLICY_CHANGE_REQUIRES_REVIEW')
        db.execute('INSERT OR IGNORE INTO allocation_policy VALUES (?,?,?,?)',(binding.account,binding.environment,binding.risk_version,self.configuration))

    def read(self,db,trade_id):
        row=db.execute('SELECT payload,digest FROM allocation_plans WHERE trade_id=?',(trade_id,)).fetchone()
        if row is None:raise ApplicationError('ALLOCATION_PLAN_NOT_FOUND',404)
        plan=AllocationPlan(row['payload'])
        if plan.digest!=row['digest']:raise ApplicationError('ALLOCATION_PLAN_INTEGRITY_FAILURE')
        return plan

    def prepare(self,app,db,now,command,trade_id,broker):
        self._schema(db,app)
        held=list(db.execute("SELECT capital,stop_risk,delta_notional FROM allocation_reservations WHERE state='HELD'"))
        if held:raise ApplicationError('PRIOR_ALLOCATION_RECONCILIATION_REQUIRED')
        sums=[sum((amount(row[index]) for row in held),Decimal(0)) for index in range(3)]
        evidence=self.provider.snapshot(command.direction)
        if not isinstance(evidence,AllocationEvidence) or evidence.binding!=app.policy.binding or evidence.generation!=broker.generation:
            raise ApplicationError('ALLOCATION_BROKER_CONTEXT_MISMATCH')
        try:
            if self.readiness is not None:
                plan=self.readiness.confirm(command,trade_id,evidence,datetime.fromtimestamp(now,timezone.utc),sums)
            else:plan=build_allocation(command,trade_id,app.policy.binding,self.policy,evidence,datetime.fromtimestamp(now,timezone.utc),*sums)
        except ApplicationError as exc:
            if hasattr(exc,'admission_decision'):
                app.security.store.audit(db,'EXIT_CAPACITY_REJECTED',decision=exc.admission_decision)
            raise
        except (PaperBrokerError,EvidenceError,ValueError,TypeError,AttributeError) as exc:
            raise ApplicationError('ALLOCATION_EVIDENCE_REJECTED') from exc
        if app.protection.emergency.blocked():raise ApplicationError('EMERGENCY_BLOCKS_ALLOCATION')
        report=plan.report()
        # Validate fully BEFORE writes; expected security denials commit audits.
        db.execute('INSERT INTO allocation_plans VALUES (?,?,?)',(trade_id,plan.payload_json,plan.digest))
        db.execute("INSERT INTO allocation_reservations VALUES (?,?,?,?,'HELD')",(trade_id,report['reserved_capital'],report['planned_stop_risk'],report['delta_notional']))
        app.security.store.audit(db,'ALLOCATION_RESERVED',trade_id=trade_id,plan_digest=plan.digest)
        return plan

    def _engine(self,app,engine):
        from executor_portfolio import OfflinePortfolioLedger
        if not isinstance(engine,OfflinePortfolioLedger) or engine.approval.account!=app.policy.binding.account:
            raise ApplicationError('ISOLATED_APPROVED_OFFLINE_PORTFOLIO_REQUIRED')
        if engine.portfolio['policy']!=self.policy.normalized()['execution']:
            raise ApplicationError('SCHEDULER_EXECUTION_POLICY_MISMATCH')

    def _entry(self,app,db,now,session,csrf,trade_id,engine,touch_session=True):
        user=app.security.require_session(db,now,session,csrf,write=True,touch=touch_session)
        broker=app._broker(now,preflight=True)
        grant=app.security.require_grant(db,now,user,broker.generation)
        row=db.execute('SELECT * FROM commands WHERE trade_id=?',(trade_id,)).fetchone()
        account=app._account_row(db)
        context=engine.portfolio.get('broker_context')
        engine_context_valid=(engine.state['generation']==broker.generation) if context is None else (
            context['broker_generation']==broker.generation and context['engine_generation']==engine.state['generation']
            and engine.session is not None and context['session_nonce']==engine.session.nonce
            and context['session_nonce']==broker.evidence_ref)
        if (row is None or row['user_id']!=user['user_id'] or row['grant_id']!=grant['grant_id']
                or row['generation']!=broker.generation or not engine_context_valid
                or row['state'] in ('COMPLETED','REJECTED_CONFIRMED','RECOVERY_REQUIRED')
                or account['blocked'] or app.protection.emergency.blocked() or engine.emergency.blocked()
                or engine.portfolio['halted'] or engine.portfolio['exit_reason']):
            raise ApplicationError('ALLOCATION_ACQUISITION_NOT_AUTHORIZED')
        reservation=db.execute('SELECT state FROM allocation_reservations WHERE trade_id=?',(trade_id,)).fetchone()
        if reservation is None or reservation[0]!='HELD':raise ApplicationError('FUNDED_RESERVATION_REQUIRED')
        return self.read(db,trade_id)

    def _binding(self,db,trade_id,engine):
        row=db.execute('SELECT journal_path,journal_id FROM allocation_bindings WHERE trade_id=?',(trade_id,)).fetchone()
        expected=(str(engine.path.resolve()),engine.portfolio.get('allocation_journal_id'))
        if row is None or tuple(row)!=expected:raise ApplicationError('ALLOCATION_JOURNAL_BINDING_MISMATCH')

    def _finance(self,app,db,now,plan,engine,account):
        report=plan.report();evidence=self.provider.snapshot(report['direction'])
        if (not isinstance(evidence,AllocationEvidence) or evidence.origin!='OFFLINE_FIXTURE'
                or evidence.binding!=app.policy.binding or evidence.generation!=engine.portfolio.get('broker_context',{}).get('broker_generation',engine.state['generation'])
                or asdict(evidence.account)!=asdict(account)):
            raise ApplicationError('CURRENT_ALLOCATION_FINANCIAL_EVIDENCE_REQUIRED')
        held=list(db.execute("SELECT capital,stop_risk,delta_notional FROM allocation_reservations WHERE state='HELD' AND trade_id!=?",(report['trade_id'],)))
        sums=[sum((amount(row[i]) for row in held),Decimal(0)) for i in range(3)]
        try:
            cap,cash,risk,delta=financial_room(report['policy'],evidence,app.policy.binding,datetime.fromtimestamp(now,timezone.utc),*sums)
            for q in evidence.quotes:
                if str(q.option.contract.con_id) not in engine.portfolio['targets']:continue
                seen=engine.portfolio['quote_history'].get(q.quote_id)
                if seen is not None and seen!=fixture_payload(q):
                    raise ApplicationError('PROVIDER_QUOTE_ID_CONFLICT_FENCES_ACQUISITION')
            if report.get('prepared_entry'):
                # Admission is not a perpetual authorization for its old quote.
                # Feed currently observed held-contract evidence through the
                # original validator before planning or dispatching more BUYs.
                current={q.option.contract.con_id:q for q in evidence.quotes}
                for cid,target in engine.portfolio['targets'].items():
                    q=current.get(int(cid))
                    if q is None or asdict(q.option.contract)!=target['contract']:
                        raise ApplicationError('CURRENT_PREPARED_CONTRACT_EVIDENCE_REQUIRED')
                    assess_liquidity(q,report['direction'],datetime.fromtimestamp(now,timezone.utc),evidence.now_monotonic,
                        lambda t:t.strftime('%Y%m%d')==report['expiration'],engine.portfolio['policy'])
                    payload=fixture_payload(q);seen=engine.portfolio['quote_history'].get(q.quote_id)
                    if seen is not None and seen!=payload:
                        raise ApplicationError('PROVIDER_QUOTE_ID_CONFLICT_FENCES_ACQUISITION')
                    # A replayed provider snapshot cannot overwrite a newer
                    # independently streamed observation. Check both below.
                    if seen is None and payload!=engine.portfolio['quotes'].get(cid):
                        previous=engine.portfolio['quotes'].get(cid)
                        if previous:
                            older=any(datetime.fromisoformat(payload[section]['source_time'])<datetime.fromisoformat(previous[section]['source_time'])
                                or payload[section]['received_monotonic']<previous[section]['received_monotonic'] for section in ('option','spx'))
                            older=older or any(payload[k]<previous[k] for k in ('sizes_source_time','sizes_received_monotonic','greeks_source_time','greeks_received_monotonic'))
                            if older:raise ApplicationError('PROVIDER_BACKWARD_EVIDENCE_FENCES_ACQUISITION')
                        engine.observe_liquidity(engine.state['generation'],'prepared-live:'+hashlib.sha256(q.quote_id.encode()).hexdigest(),q)
                if engine.portfolio['halted'] or engine.portfolio['exit_reason']:
                    raise ApplicationError('UPDATED_MARKET_FENCES_ACQUISITION')
            from executor_exit_capacity import require_capacity
            require_capacity(report,evidence,datetime.fromtimestamp(now,timezone.utc))
            # The provider/account snapshot and independent market stream must
            # BOTH support the funded target. Neither can overwrite or conceal
            # deterioration in the other. Only validated engine-held quotes are
            # substituted, never receipt/source timestamps.
            stream_quotes={int(cid):fixture_from_payload(q) for cid,q in engine.portfolio['quotes'].items()}
            streamed=replace(evidence,quotes=tuple(stream_quotes.get(q.option.contract.con_id,q) for q in evidence.quotes))
            require_capacity(report,streamed,datetime.fromtimestamp(now,timezone.utc))
            current_delta=Decimal(0)
            for cid,target in engine.portfolio['targets'].items():
                quote,_=engine._assess(int(cid),'BUY')
                current_delta+=target['quantity']*abs(Decimal(str(quote.greeks.delta)))*Decimal(str(quote.spx.price))*100
            if (engine._spent()+engine._cash_reserved()>amount(report['reserved_capital'])
                    or amount(report['reserved_capital'])>cap or max(Decimal(0),amount(report['reserved_capital'])-engine._spent())>cash
                    or amount(report['planned_stop_risk'])>risk or max(amount(report['delta_notional']),current_delta)>delta):
                raise ApplicationError('CURRENT_CAPITAL_OR_AGGREGATE_RISK_EXCEEDED')
        except ApplicationError:raise
        except (PaperBrokerError,EvidenceError,ValueError,TypeError,AttributeError,ArithmeticError) as exc:
            raise ApplicationError('CURRENT_ALLOCATION_FINANCIAL_EVIDENCE_REJECTED') from exc

    def bind_engine(self,app,session,csrf,trade_id,engine,account):
        self._engine(app,engine)
        # Persist a journal identity before registering it, and register it BEFORE
        # configuring targets. A crash can park a reservation, never clone a plan.
        with engine.lock:
            if not engine.portfolio.get('allocation_journal_id'):
                if engine.portfolio['targets'] or engine.state['intents']:raise ApplicationError('UNCONFIGURED_ALLOCATION_JOURNAL_REQUIRED')
                engine._append('allocation-identity','PORT_ALLOCATION_ID',{'journal_id':str(uuid.uuid4())})
            def register(db,now):
                plan=self._entry(app,db,now,session,csrf,trade_id,engine)
                old=db.execute('SELECT journal_path,journal_id FROM allocation_bindings WHERE trade_id=?',(trade_id,)).fetchone()
                pair=(str(engine.path.resolve()),engine.portfolio['allocation_journal_id'])
                if old is not None and tuple(old)!=pair:raise ApplicationError('PLAN_ALREADY_BOUND_TO_ANOTHER_JOURNAL')
                collision=db.execute('SELECT trade_id FROM allocation_bindings WHERE journal_path=? OR journal_id=?',pair).fetchone()
                if collision is not None and collision[0]!=trade_id:raise ApplicationError('JOURNAL_ALREADY_BOUND_TO_ANOTHER_PLAN')
                db.execute('INSERT OR IGNORE INTO allocation_bindings VALUES (?,?,?)',(trade_id,*pair))
                return plan
            app.security.store.transact(register)
            def configure(db,now):
                plan=self._entry(app,db,now,session,csrf,trade_id,engine);self._binding(db,trade_id,engine)
                if engine.portfolio.get('allocation'):
                    if engine.portfolio['allocation']['plan_digest']!=plan.digest:raise ApplicationError('SCHEDULER_PLAN_MISMATCH')
                    return plan.report()
                report=plan.report();wall=datetime.fromtimestamp(now,timezone.utc)
                quotes=[fixture_from_payload(q) for q in report['market_evidence']['quotes']]
                for quote in quotes:
                    assess_liquidity(quote,report['direction'],wall,engine.monotonic_clock(),lambda t:t.strftime('%Y%m%d')==report['expiration'],engine.portfolio['policy'])
                targets=[(VerifiedOptionContract(**line['contract']),line['quantity']) for line in report['lines'] if line['quantity']]
                engine.configure(engine.state['generation'],targets,report['entry_spx'],account,lambda t:t.strftime('%Y%m%d')==report['expiration'],allocation_plan=plan)
                for quote in quotes:
                    if quote.option.contract.con_id in {c.con_id for c,_ in targets}:
                        engine.observe_liquidity(engine.state['generation'],'allocation-quote:'+hashlib.sha256(quote.quote_id.encode()).hexdigest(),quote)
                return report
            return app.security.store.transact(configure)

    def _mirror_emergency(self,app,engine):
        if app.protection.emergency.blocked():
            request=app.protection.emergency.status()['request_id'] or 'unresolved'
            engine.trigger_exit('allocation-emergency:'+hashlib.sha256(request.encode()).hexdigest(),'EMERGENCY_EXIT_ALL',True)

    def advance(self,app,session,csrf,trade_id,engine,account,background=False):
        """Plan simulated intents only; fresh original session/grant required."""
        self._engine(app,engine)
        with engine.lock:
            self._mirror_emergency(app,engine)
            def schedule(db,now):
                self._binding(db,trade_id,engine)
                plan=self.read(db,trade_id)
                if not engine.portfolio.get('allocation') or engine.portfolio['allocation']['plan_digest']!=plan.digest:
                    raise ApplicationError('SCHEDULER_PLAN_MISMATCH')
                self._entry(app,db,now,session,csrf,trade_id,engine,touch_session=not background)
                self._finance(app,db,now,plan,engine,account)
                if engine._spent()+engine._cash_reserved()>amount(plan.report()['reserved_capital']):
                    engine._halt('AUTHORIZED_ALLOCATION_RESERVATION_EXCEEDED')
                return engine.schedule(engine.state['generation'],account)
            try:return app.security.store.transact(schedule)
            except ApplicationError:
                # Never abandon protection or assume cancellation/flat on expiry.
                engine._halt('ALLOCATION_AUTHORIZATION_OR_EMERGENCY_BLOCK')
                raise

    def simulate_dispatch(self,app,session,csrf,trade_id,engine,intent_ids,account,first_order_id=None,background=False):
        """Bind mock IDs through the existing fake-only ledger, never a broker."""
        self._engine(app,engine)
        with engine.lock:
            self._mirror_emergency(app,engine)
            def dispatch(db,now):
                self._binding(db,trade_id,engine);plan=self._entry(app,db,now,session,csrf,trade_id,engine,touch_session=not background)
                self._finance(app,db,now,plan,engine,account)
                if any(engine.state['intents'][key]['side']!='BUY' for key in intent_ids):
                    raise ApplicationError('ALLOCATION_DISPATCH_IS_ENTRY_ONLY')
                return engine.dispatch_batch(engine.state['generation'],intent_ids,account,first_order_id=first_order_id)
            try:return app.security.store.transact(dispatch)
            except ApplicationError:
                engine._halt('ALLOCATION_AUTHORIZATION_OR_EMERGENCY_BLOCK');raise

    def release_reconciled(self,app,service_token,trade_id,engine):
        """Service authority may RELEASE cash, never create acquisition intents."""
        app.protection.require(service_token,'RECONCILE');self._engine(app,engine)
        with engine.lock:
            engine._planning_gate(engine.state['generation'])
            if engine.positions() or any(not i['terminal_proven'] for i in engine.state['intents'].values()):
                raise ApplicationError('BROKER_CONFIRMED_FLAT_REQUIRED')
            from executor_execution_coordinator import unresolved_protection_orders
            if unresolved_protection_orders(engine):raise ApplicationError('PROTECTION_ORDERS_RECONCILIATION_REQUIRED')
            # An untouched configured plan is still authorized acquisition. Seal
            # that sequence durably before releasing its remaining reservation.
            engine._halt('ALLOCATION_SEQUENCE_CLOSED')
            def release(db,now):
                self._binding(db,trade_id,engine)
                plan=self.read(db,trade_id)
                if not engine.portfolio.get('allocation') or engine.portfolio['allocation']['plan_digest']!=plan.digest:
                    raise ApplicationError('SCHEDULER_PLAN_MISMATCH')
                row=db.execute('SELECT state FROM commands WHERE trade_id=?',(trade_id,)).fetchone()
                if row is None or row[0] not in ('COMPLETED','REJECTED_CONFIRMED'):
                    raise ApplicationError('APPLICATION_RECONCILIATION_REQUIRED')
                db.execute("UPDATE allocation_reservations SET state='RELEASED' WHERE trade_id=?",(trade_id,))
                app.security.store.audit(db,'ALLOCATION_RESERVATION_RELEASED',trade_id=trade_id,plan_digest=plan.digest)
                return {'state':'RELEASED','submission_enabled':False}
            return app.security.store.transact(release)
