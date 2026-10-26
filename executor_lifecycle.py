"""Unified OFFLINE lifecycle. No IBKR client, real orders, listener or execution.

Entry leases exist in memory only. Service capabilities supervise/reconcile/reduce
without dashboard credentials; a restart permanently seals unfinished acquisition.
"""
from dataclasses import asdict,replace
from datetime import datetime,timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path
from threading import RLock,Event,Thread

from executor_application import UnifiedApplication
from executor_allocation_service import AllocationBoundary
from executor_offline_fixture import OfflineBrokerFixture,order_record,execution_record
from executor_protection_checkpoint import ProtectiveCheckpoint
from executor_paper_broker import PaperBrokerLedger,utc
from executor_paper_types import PaperSessionEvidence,PaperBrokerError,CommissionRecord,COMPONENTS
from executor_portfolio import OfflinePortfolioLedger
from mortificatio_models import ApplicationError,BrokerAdmissionEvidence,ManagedTradeEvidence,AccountBinding,TradeCommand
from mortificatio_protection import IndependentProtectionService
from mortificatio_store import encode,private_database
from simulation_evidence import EvidenceClock


class _VerifiedFixtureBoundary:
    def __init__(self,lifecycle,upstream):self.lifecycle,self.upstream=lifecycle,upstream

    def admission_evidence(self):
        owner=self.lifecycle
        if owner.checkpoint.read()['fenced'] or owner.critical:
            raise ApplicationError('PROTECTIVE_HEALTH_FENCES_ENTRY')
        if owner.start_worker and (owner.thread is None or not owner.thread.is_alive()):
            raise ApplicationError('INDEPENDENT_SUPERVISION_REQUIRED')
        owner.anchor._session(owner.anchor.state['generation'])
        if not owner.fixture.connected or not owner._fresh_account():raise ApplicationError('FRESH_ACCOUNT_RECONCILIATION_REQUIRED')
        evidence=self.upstream.admission_evidence()
        if not isinstance(evidence,BrokerAdmissionEvidence) or evidence.generation!=owner.anchor.state['generation']:
            raise ApplicationError('SYNTHETIC_SESSION_GENERATION_MISMATCH')
        return replace(evidence,evidence_ref=owner.anchor.session.nonce)


class OfflineExecutionLifecycle:
    def __init__(self,application,root,approval,fixture,service_token,wall_clock,monotonic_clock,fault_hook=None,start_worker=True):
        if (not isinstance(application,UnifiedApplication) or not isinstance(application.allocation,AllocationBoundary)
                or not isinstance(fixture,OfflineBrokerFixture) or fixture.origin!='OFFLINE_FIXTURE'
                or fixture.account!=application.policy.binding.account or approval.account!=fixture.account):
            raise ApplicationError('EXPLICIT_OFFLINE_LIFECYCLE_DEPENDENCIES_REQUIRED')
        application.protection.require(service_token,'RECONCILE');application.protection.require(service_token,'OBSERVE_RISK')
        self.app,self.security,self.allocation=application,application.security,application.allocation
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.fixture,self.token,self.approval=fixture,service_token,approval
        self.wall_clock,self.monotonic_clock=wall_clock,monotonic_clock
        self.lock=RLock();self.engines={};self.leases={};self.proof=None;self.last_error=None
        self.stop_event=Event();self.thread=None;self.fault_hook=fault_hook or (lambda stage,trade:None)
        self.closed=False;self.critical=None;self.foundation_available=True
        if type(start_worker) is not bool:raise ApplicationError('EXPLICIT_SUPERVISION_MODE_REQUIRED')
        self.start_worker=start_worker
        lock_path=private_database(self.root/'lifecycle.lock');self.lock_file=lock_path.open('a+b')
        try:fcntl.flock(self.lock_file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close();raise ApplicationError('LIFECYCLE_WRITER_ALREADY_ACTIVE')
        self.upstream=application.broker
        try:
            self.checkpoint=ProtectiveCheckpoint(self.root/'protection.sqlite',asdict(application.policy.binding),self.root,
                lambda:utc(self.wall_clock()).timestamp())
            self.anchor=PaperBrokerLedger(self.root/'session.sqlite',approval,wall_clock,monotonic_clock)
            try:self._schema()
            except Exception:
                if not self.checkpoint.read()['initialized']:raise
                self._fence('FOUNDATION_UNAVAILABLE')
            self._adopt(recovery=True)
            self.history=None
            self._reporting()
            application.broker=_VerifiedFixtureBoundary(self,self.upstream)
            if start_worker:self.start_supervision(service_token)
        except BaseException:
            if isinstance(application.broker,_VerifiedFixtureBoundary) and application.broker.lifecycle is self:
                application.broker=self.upstream
            for engine in self.engines.values():engine.close()
            if hasattr(self,'anchor'):self.anchor.close()
            self.lock_file.close();raise

    def _schema(self):
        def initialize(db,now):
            db.execute('CREATE TABLE IF NOT EXISTS execution_service (singleton INTEGER PRIMARY KEY CHECK(singleton=1),root TEXT NOT NULL,next_order_id INTEGER NOT NULL,callback_cursor INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO execution_service VALUES (1,?,1000,0)',(str(self.root),))
            if db.execute('SELECT root FROM execution_service').fetchone()[0]!=str(self.root):raise ApplicationError('LIFECYCLE_ROOT_ALREADY_BOUND')
            db.execute('CREATE TABLE IF NOT EXISTS execution_lifecycle (trade_id TEXT PRIMARY KEY,journal_path TEXT UNIQUE NOT NULL,state TEXT NOT NULL,reason TEXT,revision INTEGER NOT NULL,fingerprint TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS execution_publications (trade_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,delivered INTEGER NOT NULL,PRIMARY KEY(trade_id,revision))')
        self.security.store.transact(initialize)

    def _ensure_open(self):
        if self.stop_event.is_set():raise ApplicationError('LIFECYCLE_CLOSED')

    def _state(self,trade,state,reason=None):
        self._foundation(lambda:self.security.store.transact(lambda db,now:db.execute('UPDATE execution_lifecycle SET state=?,reason=? WHERE trade_id=?',(state,reason,trade))))

    def _fence(self,condition,critical=False):
        # A volatile flag is an alarm only, never replacement trading state.
        self.last_error=condition;self.leases.clear()
        if condition=='FOUNDATION_UNAVAILABLE':self.foundation_available=False
        try:self.checkpoint.fence(condition,critical)
        except Exception:
            critical=True
            # Independent latch is a second durable entry fence if checkpoint
            # storage itself fails. Do not claim this write succeeded.
            try:self.app.protection.emergency.request('protective-storage-failure')
            except Exception:pass
        if critical:self.critical='CRITICAL_UNRESOLVED_PROTECTION'
        for engine in self.engines.values():
            try:engine._halt(condition)
            except Exception:self.critical='CRITICAL_UNRESOLVED_PROTECTION'

    def _foundation(self,operation):
        try:
            result=operation();self.foundation_available=True;return result
        except (sqlite3.Error,OSError,ApplicationError):
            self._fence('FOUNDATION_UNAVAILABLE');return None

    def _reporting(self):
        if not self._foundation_ready():return
        def report():
            self._mirror_checkpoint()
            if self.history is None:
                from executor_trade_history import TradeHistoryService
                self.history=TradeHistoryService(self)
            self._publish_all()
        self._foundation(report)

    def _foundation_ready(self):
        """No waiting for an application writer on the protective path."""
        try:
            with self.security.store.database() as db:
                db.execute('PRAGMA busy_timeout=0')
                db.execute('BEGIN IMMEDIATE')
                # No authority inferred here; every entry still fully verifies
                # authentication, audit integrity, account and risk afterward.
                db.execute('ROLLBACK')
            return True
        except (sqlite3.Error,OSError,ApplicationError):
            self._fence('FOUNDATION_UNAVAILABLE');return False

    def _block(self,reason):
        self.last_error=reason;self.proof=None
        def block(db,now):
            self.app._account_row(db)
            db.execute('UPDATE account_state SET blocked=1 WHERE account=? AND environment=?',(self.app.policy.binding.account,self.app.policy.binding.environment))
            self.security.store.audit(db,'LIFECYCLE_RECOVERY_REQUIRED',code=reason)
        self._fence(reason,critical=True)
        self._foundation(lambda:self.security.store.transact(block))

    def _adopt(self,recovery=False,sync=True):
        def rows(db,now):
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='allocation_plans'").fetchone()
            if not exists:return []
            result=[]
            for row in db.execute('SELECT trade_id FROM allocation_plans'):
                trade=row[0];path=str(self.root/(trade+'.sqlite'))
                db.execute("INSERT OR IGNORE INTO execution_lifecycle VALUES (?,?,'RESERVED',NULL,0,NULL)",(trade,path))
                binding=db.execute('SELECT journal_path FROM allocation_bindings WHERE trade_id=?',(trade,)).fetchone()
                registered=db.execute('SELECT journal_path FROM execution_lifecycle WHERE trade_id=?',(trade,)).fetchone()[0]
                result.append((trade,registered,binding[0] if binding else None))
            return result
        def inventory():
            result=self.security.store.transact(rows)
            with self.security.store.database() as db:
                next_id,cursor=db.execute('SELECT next_order_id,callback_cursor FROM execution_service').fetchone()
            self.checkpoint.import_inventory(result,next_id,cursor)
        if sync:self._foundation(inventory)
        state=self.checkpoint.read()
        if not state['initialized']:raise ApplicationError('PROTECTIVE_INVENTORY_UNAVAILABLE',503)
        for trade,(path,bound) in state['trades'].items():
            if trade in self.engines:continue
            expected=self.root/(trade+'.sqlite')
            if Path(path)!=expected or bound is not None and Path(bound)!=expected:
                self._state(trade,'RECOVERY_REQUIRED','UNREVIEWED_EXISTING_JOURNAL');self._block('UNREVIEWED_EXISTING_JOURNAL');continue
            if (bound is not None or recovery) and not expected.exists():
                self._state(trade,'RECOVERY_REQUIRED','MISSING_BOUND_JOURNAL');self._block('MISSING_BOUND_JOURNAL');continue
            try:engine=OfflinePortfolioLedger(expected,self.approval,self.allocation.policy.execution,self.wall_clock,self.monotonic_clock)
            except Exception:
                self._fence('PROTECTIVE_JOURNAL_UNAVAILABLE',critical=True)
                raise
            self.engines[trade]=engine
            if recovery:
                engine._halt('RESTART_SEALS_ACQUISITION');self._state(trade,'RECOVERY_REQUIRED','RESTART_RECONCILIATION_REQUIRED')
            if self.anchor.session is not None:self._attach(engine)

    def _attach(self,engine):
        evidence=self.anchor.session
        if evidence is None:raise ApplicationError('VERIFIED_SYNTHETIC_SESSION_REQUIRED')
        self.anchor._session(self.anchor.state['generation'])
        # This is local verification of an active synthetic session, never an
        # exchange timestamp or a new IBKR login/2FA assertion.
        engine.verify_session(replace(evidence,verified_at=utc(self.wall_clock())))
        engine._append('broker-context:'+str(engine.state['generation']),'PORT_BROKER_CONTEXT',dict(
            engine_generation=engine.state['generation'],broker_generation=self.anchor.state['generation'],session_nonce=evidence.nonce))

    def verify_broker_session(self,service_token,evidence):
        self._ensure_open()
        self.app.protection.require(service_token,'RECONCILE')
        with self.lock:
            if not isinstance(evidence,PaperSessionEvidence):raise ApplicationError('PAPER_SESSION_EVIDENCE_REQUIRED')
            generation=self.anchor.verify_session(evidence)
            self.fixture.connected=True;self.proof=None
            for engine in self.engines.values():self._attach(engine)
            self._callbacks();self._reconcile_all();self._reporting()
            return {'origin':'OFFLINE_FIXTURE','verified_synthetic_session':True,'real_ibkr_authenticated':False,'generation':generation,'submission_enabled':False}

    def disconnect(self,service_token):
        self._ensure_open()
        self.app.protection.require(service_token,'RECONCILE')
        with self.lock:
            self.fixture.connected=False;self.proof=None;self.leases.clear()
            if self.anchor.session is not None:self.anchor.disconnect(self.anchor.state['generation'])
            for trade,engine in self.engines.items():
                engine._halt('BROKER_DISCONNECTED')
                if engine.session is not None:engine.disconnect(engine.state['generation'])
                self._state(trade,'PENDING_RECONCILIATION','BROKER_DISCONNECTED')

    def _fresh_account(self):
        if self.proof is None or not self.fixture.connected or self.anchor.session is None:return False
        try:
            self.anchor._session(self.anchor.state['generation'])
            receipts=dict(self.proof.component_receipts)
            if not all(0<=self.monotonic_clock()-value<=1 for value in receipts.values()):return False
            for engine in self.engines.values():engine._planning_gate(engine.state['generation'])
            return True
        except (PaperBrokerError,ApplicationError):return False

    def _callbacks(self):
        cursor=self.checkpoint.read()['callback_cursor']
        for sequence,trade,kind,raw,stamp in self.fixture.callbacks_after(cursor):
            engine=self.engines.get(trade)
            if engine is None:self._block('UNMANAGED_BROKER_CALLBACK');raise ApplicationError('UNMANAGED_BROKER_CALLBACK')
            event='fixture:'+str(sequence);data=json.loads(raw);generation=engine.state['generation']
            try:
                if kind=='ORDER':engine.observe_order(generation,event,order_record(data))
                elif kind=='COMMISSION':engine.observe_commission(generation,event,CommissionRecord(**data))
                elif kind=='EXECUTION':
                    record=execution_record(data)
                    if not stamp:raise ApplicationError('SOURCE_EXECUTION_TIME_REQUIRED')
                    time=utc(datetime.fromisoformat(stamp))
                    if time>utc(self.wall_clock()):raise ApplicationError('FUTURE_SOURCE_EXECUTION_TIME')
                    metadata=dict(exec_id=record.exec_id,record_digest=hashlib.sha256(raw.encode()).hexdigest(),executed_at=time.isoformat())
                    engine._append('fill-source:'+hashlib.sha256(record.exec_id.encode()).hexdigest(),'PORT_FILL_SOURCE_TIME',metadata)
                    engine.observe_execution(generation,event,record)
                else:raise ApplicationError('UNKNOWN_FIXTURE_CALLBACK')
            except Exception:
                engine._halt('CALLBACK_REQUIRES_RECOVERY');self._block('BROKER_CALLBACK_REJECTED');raise
            self.checkpoint.cursor(sequence)

    def _mirror_checkpoint(self):
        state=self.checkpoint.read()
        def mirror():
            def update(db,now):
                db.execute('UPDATE execution_service SET callback_cursor=?,next_order_id=?',
                    (state['callback_cursor'],state['next_order_id']))
                if state['fenced']:
                    self.app._account_row(db)
                    db.execute('UPDATE account_state SET blocked=1 WHERE account=? AND environment=?',
                        (self.app.policy.binding.account,self.app.policy.binding.environment))
            self.security.store.transact(update)
        self._foundation(mirror)

    def _reconcile_all(self):
        self.proof=None
        if not self.fixture.connected or self.anchor.session is None:return False
        snapshot=self.fixture.snapshot(self.anchor.state['generation'])
        try:
            if (snapshot.account!=self.approval.account or snapshot.generation!=self.anchor.state['generation']
                    or snapshot.scope!='ALL_APPROVED_ACCOUNT_INTENTS' or len(snapshot.component_receipts)!=len(COMPONENTS)
                    or set(snapshot.complete_components)!=set(COMPONENTS)):
                raise ApplicationError('COMPLETE_ACCOUNT_SNAPSHOT_REQUIRED')
            refs={};executions={};expected={};owners={};contracts={}
            for trade,engine in self.engines.items():
                for intent in engine.state['intents'].values():
                    if intent['order_ref'] in refs:raise ApplicationError('GLOBAL_ORDER_REF_COLLISION')
                    refs[intent['order_ref']]=trade
                for exec_id in engine.state['executions']:
                    if exec_id in executions:raise ApplicationError('GLOBAL_EXECUTION_ID_COLLISION')
                    executions[exec_id]=trade
                for cid,quantity in engine.positions().items():
                    if cid in owners:raise ApplicationError('OVERLAPPING_TRADE_EXPOSURE_REQUIRES_RECOVERY')
                    owners[cid]=trade;expected[cid]=quantity
                    contracts[cid]=next(i['contract'] for i in engine.state['intents'].values() if i['contract']['con_id']==cid)
            observed={}
            for position in snapshot.positions:
                cid=position.contract.con_id
                if (position.account!=self.approval.account or cid in observed or contracts.get(cid)!=asdict(position.contract)
                        or type(position.quantity) is not int or not 0<position.quantity<=25):
                    raise ApplicationError('UNMANAGED_OR_INVALID_ACCOUNT_POSITION')
                observed[cid]=position.quantity
            if observed!=expected or len(observed)>5 or sum(observed.values())>125:raise ApplicationError('ACCOUNT_POSITION_DISAGREEMENT')
            if len(set(snapshot.execution_ids))!=len(snapshot.execution_ids) or set(snapshot.execution_ids)!=set(executions):
                raise ApplicationError('ACCOUNT_EXECUTION_HISTORY_DISAGREEMENT')
            if any(order.order_ref not in refs for order in (*snapshot.open_orders,*snapshot.completed_orders)):
                raise ApplicationError('UNMANAGED_ACCOUNT_ORDER')
            # Partition only after exact aggregate broker evidence agrees. Shared
            # active conIds across trades are rejected rather than guessed apart.
            matched=True
            for trade,engine in self.engines.items():
                part=replace(snapshot,generation=engine.state['generation'],
                    positions=tuple(p for p in snapshot.positions if owners[p.contract.con_id]==trade),
                    open_orders=tuple(o for o in snapshot.open_orders if refs[o.order_ref]==trade),
                    completed_orders=tuple(o for o in snapshot.completed_orders if refs[o.order_ref]==trade),
                    execution_ids=tuple(e for e in snapshot.execution_ids if executions[e]==trade))
                matched=engine.reconcile(part)['matched'] and matched
            # Empty-account proof still requires the unchanged component/time gate.
            if not self.engines:matched=self.anchor.reconcile(snapshot)['matched']
            if matched:self.proof=snapshot
            return matched
        except (ApplicationError,PaperBrokerError,ValueError,TypeError,AttributeError,KeyError):
            for engine in self.engines.values():engine.reconciled_at=None;engine._halt('ACCOUNT_RECONCILIATION_FAILED')
            self._block('ACCOUNT_RECONCILIATION_FAILED');return False

    def _ids(self,count):
        first=self.checkpoint.reserve_ids(count)
        return first

    def _entry_step(self,trade):
        engine=self.engines[trade]
        lease=self.leases.get(trade)
        if lease is None or engine.portfolio['halted'] or engine.portfolio['exit_reason'] or engine._entry_filled():return
        session,csrf=lease
        try:
            direction=engine.portfolio['allocation']['direction']
            account=self.allocation.provider.snapshot(direction).account
            actions=self.allocation.advance(self.app,session,csrf,trade,engine,account,background=True)
            identities=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT' and a.get('side')=='BUY'
                        and engine.state['intents'][a['intent_id']]['order_id'] is None]
            if identities:
                if not self._reconcile_all():return
                first=self._ids(len(identities))
                self.allocation.simulate_dispatch(self.app,session,csrf,trade,engine,identities,account,first_order_id=first,background=True)
                self.fault_hook('AFTER_MODEL_BINDING',trade)
                for identity in identities:
                    self.fixture.bindings(trade,engine,[identity])
                    self.fault_hook('AFTER_FIXTURE_BINDING',trade)
                self._callbacks();self._reconcile_all()
        except (sqlite3.Error,OSError):
            self._fence('FOUNDATION_UNAVAILABLE')
        except (ApplicationError,PaperBrokerError):
            engine._halt('ENTRY_AUTHORITY_OR_BROKER_BLOCK');self.leases.pop(trade,None)
            self._state(trade,'ACQUISITION_HALTED','ENTRY_AUTHORITY_OR_BROKER_BLOCK')

    def _reduce(self,trade):
        engine=self.engines[trade]
        if not engine.portfolio['targets'] or engine.session is None:return
        worker=IndependentProtectionService(self.app.protection,engine)
        actions=worker.reduction_actions(self.token)
        if self.fixture.connected:
            self.fixture.cancel_observations(trade,engine,actions)
            self._callbacks()
            if not self._reconcile_all():return
            # A cancellation acknowledgement does not prove no late fills.
            actions=worker.reduction_actions(self.token)
            ids=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT' and a.get('side')=='SELL'
                 and engine.state['intents'][a['intent_id']]['order_id'] is None]
            if ids:
                if not self._reconcile_all():return
                worker.simulate_reduction_bindings(self.token,ids,self._ids(len(ids)))
                self.fixture.bindings(trade,engine,ids);self._callbacks();self._reconcile_all()

    def _risk_health(self):
        condition='HEALTHY_OFFLINE'
        for engine in self.engines.values():
            from executor_execution_coordinator import UnifiedExecutionCoordinator,unresolved_protection_orders
            exposed=bool(engine.positions() or any(not i['terminal_proven'] for i in engine.state['intents'].values()) or unresolved_protection_orders(engine))
            if not exposed:continue
            try:
                if not self.fixture.connected:raise ApplicationError('BROKER_DISCONNECTED')
                self.anchor._session(self.anchor.state['generation']);engine._session(engine.state['generation'])
            except Exception:
                condition='CRITICAL_UNRESOLVED_BROKER';break
            if engine.portfolio.get('protection_lifecycle') and (engine.portfolio['protection_lifecycle']['uncertain'] or
                any(UnifiedExecutionCoordinator(engine).protection_status(cid)['status']!='SYNTHETIC_COVERED' for cid in engine.positions())):
                condition='CRITICAL_UNRESOLVED_PROTECTION';break
            try:
                for cid in engine.positions():
                    quote=engine._assess(cid,'SELL')[0]
                    EvidenceClock().validate_spx(quote.spx,self.wall_clock(),self.monotonic_clock())
            except Exception:
                condition='CRITICAL_UNRESOLVED_MARKET_DATA';break
        self.checkpoint.risk_condition(condition)
        if condition!='HEALTHY_OFFLINE':
            self.leases.clear()
            for engine in self.engines.values():engine._halt(condition)

    def _drain_publications(self):
        rows=self.security.store.transact(lambda db,now:list(db.execute('SELECT trade_id,revision,payload FROM execution_publications WHERE delivered=0 ORDER BY rowid')))
        for trade,revision,raw in rows:
            data=json.loads(raw);data['binding']=AccountBinding(**data['binding'])
            data['first_fill_at']=datetime.fromisoformat(data['first_fill_at']) if data['first_fill_at'] else None
            self.app.observe_managed_trade(self.token,ManagedTradeEvidence(**data))
            self.fault_hook('AFTER_LIFECYCLE_PUBLICATION',trade)
            def acknowledge(db,now):
                state=db.execute('SELECT state FROM commands WHERE trade_id=?',(trade,)).fetchone()[0]
                db.execute('UPDATE execution_lifecycle SET state=?,reason=NULL WHERE trade_id=?',(state,trade))
                db.execute('UPDATE execution_publications SET delivered=1 WHERE trade_id=? AND revision=?',(trade,revision))
                if state=='RECOVERY_REQUIRED':db.execute("UPDATE allocation_reservations SET state='HELD' WHERE trade_id=?",(trade,))
            self.security.store.transact(acknowledge)

    def _publish_all(self):
        self._drain_publications()
        for trade,engine in self.engines.items():
            if not engine.portfolio.get('allocation'):
                self._state(trade,'RECOVERY_REQUIRED','UNCONFIGURED_RESERVATION');continue
            fresh=self._fresh_account()
            from executor_execution_coordinator import unresolved_protection_orders
            quantities=engine.positions();outstanding=sum(not i['terminal_proven'] for i in engine.state['intents'].values())+unresolved_protection_orders(engine)
            filled=[e['record'] for e in engine.state['executions'].values() if e['record']['side']=='BUY' and e['record']['quantity']>0]
            times=engine.portfolio.get('fill_source_times',{})
            if any(e['exec_id'] not in times for e in filled):self._block('MISSING_SOURCE_FILL_TIME');continue
            first=min((datetime.fromisoformat(times[e['exec_id']]['executed_at']) for e in filled),default=None)
            sealed=engine.portfolio['halted'] or engine.portfolio['exit_reason'] is not None
            complete=bool(fresh and (quantities or outstanding or sealed))
            values=dict(entry_filled=bool(filled),first_fill_at=first.isoformat() if first else None,
                position_quantity=sum(quantities.values()),outstanding_orders=outstanding,reconciliation_complete=complete,
                execution_history_complete=fresh,commissions_complete=fresh,generation=self.anchor.state['generation'])
            fingerprint=encode(values)
            def reserve_publication(db,now):
                row=db.execute('SELECT revision,fingerprint FROM execution_lifecycle WHERE trade_id=?',(trade,)).fetchone()
                if row[1]==fingerprint:return
                rev=row[0]+1
                evidence=ManagedTradeEvidence(trade,self.app.policy.binding,self.anchor.state['generation'],rev,
                    'lifecycle:'+trade+':'+str(rev),bool(filled),first,sum(quantities.values()),outstanding,complete,fresh,fresh)
                payload=asdict(evidence);payload['first_fill_at']=first.isoformat() if first else None
                db.execute('INSERT INTO execution_publications VALUES (?,?,?,0)',(trade,rev,encode(payload)))
                db.execute('UPDATE execution_lifecycle SET revision=?,fingerprint=? WHERE trade_id=?',(rev,fingerprint,trade))
            self.security.store.transact(reserve_publication)
            self._drain_publications()
            if complete and sealed and not quantities and outstanding==0:
                held=self.security.store.transact(lambda db,now:db.execute('SELECT state FROM allocation_reservations WHERE trade_id=?',(trade,)).fetchone()[0])
                if held=='HELD':self.allocation.release_reconciled(self.app,self.token,trade,engine)
                self.leases.pop(trade,None)
                self._state(trade,'FLAT_CONFIRMED')

    def direction_command(self,session,csrf,command):
        with self.lock:
            self._ensure_open()
            if not isinstance(command,TradeCommand):raise ApplicationError('DIRECTION_ONLY_COMMAND_REQUIRED',400)
            command.validate()
            if self.critical or self.checkpoint.read()['fenced']:
                raise ApplicationError('PROTECTIVE_HEALTH_FENCES_ENTRY')
            def authenticate(db,now):
                self.security.require_session(db,now,session,csrf,write=True)
                return db.execute('SELECT 1 FROM commands WHERE command_id=?',(command.command_id,)).fetchone() is not None
            duplicate=self.security.store.transact(authenticate)
            if not duplicate and self._native_unresolved():raise ApplicationError('NATIVE_PROTECTION_RECOVERY_FENCES_ENTRY')
            if not duplicate:
                self.app.protection.require(self.token,'RECONCILE');self.app.protection.require(self.token,'OBSERVE_RISK')
                if self.fixture.connected and self.anchor.session is not None:
                    self._callbacks();self._reconcile_all();self._publish_all()
            result=self.app.direction_command(session,csrf,command)
            if result['result']=='DUPLICATE':return dict(result,lifecycle=self._trade_status(result['trade_id']))
            trade=result['trade_id'];self.leases[trade]=(session,csrf)
            try:
                self.fault_hook('AFTER_RESERVATION',trade);self._adopt()
                if not self._reconcile_all():raise ApplicationError('INITIAL_RECONCILIATION_REQUIRED')
                account=self.allocation.provider.snapshot(command.direction).account
                self.allocation.bind_engine(self.app,session,csrf,trade,self.engines[trade],account)
                protection_version=self.checkpoint.read().get('protection_policy')
                if protection_version:
                    from executor_adaptive_liquidity import AdaptiveExitPolicy
                    from executor_liquidation import GlobalLiquidationCoordinator
                    from executor_execution_coordinator import UnifiedExecutionCoordinator
                    engine=self.engines[trade]
                    GlobalLiquidationCoordinator(engine).configure(AdaptiveExitPolicy('protection-exit-v1'))
                    UnifiedExecutionCoordinator(engine).configure_protection(protection_version)
                    from executor_exit_readiness import enable_readiness
                    enable_readiness(engine)
                self._adopt()  # Durable bound inventory before any modeled dispatch.
                self.fault_hook('AFTER_CONFIGURATION',trade)
                self._entry_step(trade);self._publish_all()
            except Exception as exc:
                self.leases.pop(trade,None)
                if trade in self.engines:self.engines[trade]._halt('ENTRY_INITIALIZATION_REQUIRES_RECOVERY')
                self._state(trade,'RECOVERY_REQUIRED','INITIALIZATION_INTERRUPTED')
                self.last_error='INITIALIZATION_INTERRUPTED'
            return dict(result,lifecycle=self._trade_status(trade))

    def supervise(self,service_token):
        self._ensure_open()
        self.app.protection.require(service_token,'RECONCILE');self.app.protection.require(service_token,'OBSERVE_RISK')
        with self.lock:
            try:
                self._adopt(recovery=True,sync=False)
                if not self.fixture.connected or self.anchor.session is None:
                    self.disconnect(service_token)
                    self._risk_health()
                    return self._status()
                self._callbacks();self._reconcile_all()
                self._risk_health()
                for trade in list(self.engines):
                    if not self.checkpoint.read()['fenced'] and self._foundation_ready():self._entry_step(trade)
                    self._reduce(trade)
                self._reconcile_all();self._reporting()
                return self._status()
            except Exception:
                self.proof=None;self._fence('SUPERVISION_REQUIRES_RECOVERY',critical=True)
                raise

    def observe_liquidity(self,service_token,trade,event_id,quote):
        self._ensure_open()
        self.app.protection.require(service_token,'OBSERVE_RISK')
        with self.lock:
            engine=self.engines[trade]
            try:engine.observe_liquidity(engine.state['generation'],event_id,quote)
            except Exception:
                self._fence('INVALID_MARKET_EVENT',critical=True);raise
            return self.supervise(service_token)

    def configure_exit_execution(self,service_token,trade,policy):
        """Local offline policy pin; never an entry permission or browser setting."""
        self._ensure_open();self.app.protection.require(service_token,'OBSERVE_RISK')
        from executor_liquidation import GlobalLiquidationCoordinator
        with self.lock:return GlobalLiquidationCoordinator(self.engines[trade]).configure(policy)

    def observe_exit_depth(self,service_token,trade,observation):
        self._ensure_open();self.app.protection.require(service_token,'OBSERVE_RISK')
        from executor_liquidation import GlobalLiquidationCoordinator
        with self.lock:return GlobalLiquidationCoordinator(self.engines[trade]).observe_depth(observation)

    def enable_exit_readiness(self,service_token,trade):
        """Offline local capability only. No trading/browser authorization."""
        self._ensure_open();self.app.protection.require(service_token,'OBSERVE_RISK')
        from executor_exit_readiness import enable_readiness
        with self.lock:return enable_readiness(self.engines[trade])

    def prepare_entry_candidates(self,service_token,directions=('CALL','PUT')):
        """Privileged synthetic feed callback, never entry permission or HTTP data."""
        self._ensure_open();self.app.protection.require(service_token,'OBSERVE_RISK')
        from executor_entry_readiness import PreparedEntries
        from executor_allocation import amount
        with self.lock:
            if self.allocation.readiness is None:
                self.allocation.readiness=PreparedEntries(self.app.policy.binding,self.allocation.policy)
            readiness=self.allocation.readiness
            def prepare(db,now):
                self.allocation._schema(db,self.app)
                rows=list(db.execute("SELECT capital,stop_risk,delta_notional FROM allocation_reservations WHERE state='HELD'"))
                held=tuple(sum((amount(row[i]) for row in rows),Decimal(0)) for i in range(3))
                result={}
                for direction in directions:
                    evidence=self.allocation.provider.snapshot(direction)
                    result[direction]=readiness.publish(direction,evidence,self.wall_clock(),readiness.sequences.get(direction,0)+1,held)
                return result
            return self.security.store.transact(prepare)

    def start_entry_readiness(self,service_token,provider):
        self._ensure_open();self.app.protection.require(service_token,'OBSERVE_RISK')
        from executor_entry_readiness import PreparedEntryWorker
        if getattr(self,'entry_worker',None) is not None:raise ApplicationError('ENTRY_WORKER_ALREADY_CONFIGURED')
        self.entry_worker=PreparedEntryWorker(self,service_token,provider)
        return self.entry_worker

    def configure_protection_lifecycle(self,service_token,trade,version):
        self._ensure_open();self.app.protection.require(service_token,'OBSERVE_RISK')
        from executor_execution_coordinator import UnifiedExecutionCoordinator
        with self.lock:return UnifiedExecutionCoordinator(self.engines[trade]).configure_protection(version)

    def configure_partial_fill_protection(self,service_token,version):
        """Local immutable OFFLINE policy before entries; never native order enablement."""
        self._ensure_open();self.app.protection.require(service_token,'OBSERVE_RISK')
        if not isinstance(version,str) or not version or len(version)>120:raise ApplicationError('PROTECTION_POLICY_VERSION_REQUIRED')
        with self.lock:
            if any(e.state['intents'] for e in self.engines.values()):raise ApplicationError('CONFIGURE_BEFORE_ENTRIES')
            def configure(state):
                if state.get('protection_policy') not in (None,version):raise ApplicationError('PROTECTION_POLICY_CHANGE_REQUIRES_REVIEW')
                state['protection_policy']=version
            return self.checkpoint.update(configure)

    def observe_synthetic_stop(self,service_token,trade,evidence):
        self._ensure_open();self.app.protection.require(service_token,'RECONCILE')
        from executor_execution_coordinator import UnifiedExecutionCoordinator
        with self.lock:return UnifiedExecutionCoordinator(self.engines[trade]).observe_stop(evidence)

    def _native_unresolved(self):
        from executor_native_protection import NativeProtectionCoordinator
        return any(e.portfolio.get('native_protection') and NativeProtectionCoordinator(e).status()['unresolved'] for e in self.engines.values())

    def _native_state(self,engine):
        from executor_native_protection import NativeProtectionCoordinator
        return NativeProtectionCoordinator(engine).status()['condition'] if engine.portfolio.get('native_protection') else 'LEGACY_OFFLINE_RESEARCH'

    def prepare_native_protection(self,service_token,trade,cid,request_id,replaces=None):
        self._ensure_open();self.app.protection.require(service_token,'OBSERVE_RISK')
        from executor_native_protection import NativeProtectionCoordinator
        with self.lock:return NativeProtectionCoordinator(self.engines[trade]).plan(cid,request_id,replaces)

    def bind_native_simulation(self,service_token,trade,intent_id):
        self._ensure_open();self.app.protection.require(service_token,'RECONCILE')
        from executor_native_protection import NativeProtectionCoordinator
        with self.lock:
            e=self.engines[trade]
            self._callbacks()
            if not self._reconcile_all():raise ApplicationError('NATIVE_BINDING_REQUIRES_RECONCILIATION')
            order_id=self.checkpoint.reserve_ids(1)
            NativeProtectionCoordinator(e).bind_synthetic(intent_id,order_id)
            self.fixture.bindings(trade,e,[intent_id]);self._callbacks();self._reconcile_all()
            return NativeProtectionCoordinator(e).status()

    def observe_spx(self,service_token,trade,event_id,spx):
        self._ensure_open()
        self.app.protection.require(service_token,'OBSERVE_RISK')
        with self.lock:
            try:
                # Fill callbacks already available in the fixture are observed
                # first. Valid SPX ticks before ownership do not arm protection.
                self._callbacks()
                if self.engines[trade].positions():
                    IndependentProtectionService(self.app.protection,self.engines[trade]).observe_spx(service_token,event_id,spx)
                else:EvidenceClock().validate_spx(spx,self.wall_clock(),self.monotonic_clock())
            except Exception:
                self._fence('INVALID_MARKET_EVENT',critical=True);raise
            return self.supervise(service_token)

    def emergency_exit_all(self,session,csrf,request_id):
        self._ensure_open()
        result=self.app.emergency_exit_all(session,csrf,request_id)
        self.supervise(self.token)
        return result

    def begin_trading_authorization(self,*args):
        with self.lock:
            self._ensure_open();return self.app.begin_trading_authorization(*args)

    def dashboard_session(self,session):
        with self.lock:
            self._ensure_open();return self.app.dashboard_session(session)

    def command_status(self,session,command_id):
        with self.lock:
            self._ensure_open();result=self.app.command_status(session,command_id)
            return dict(result,lifecycle=self._trade_status(result['trade_id']))
    def finish_trading_authorization(self,*args):
        with self.lock:
            self._ensure_open();return self.app.finish_trading_authorization(*args)

    def _trade_status(self,trade):
        engine=self.engines.get(trade)
        if engine is None:return {'trade_id':trade,'state':'RECOVERY_REQUIRED','reconciled_flat':False,
                                  'open_contracts':None,'pending_orders':None,'entry_blocked':True,'submission_enabled':False}
        fresh=self._fresh_account();positions=engine.positions();p=engine.portfolio
        terminal=not positions and all(i['terminal_proven'] for i in engine.state['intents'].values())
        from executor_execution_coordinator import unresolved_protection_orders
        terminal=terminal and not unresolved_protection_orders(engine)
        sealed=p['halted'] or p['exit_reason'] is not None
        funded=bool(p.get('allocation'))
        reservation=command=registry=None
        try:
            with self.security.store.database() as db:
                reservation=db.execute('SELECT state FROM allocation_reservations WHERE trade_id=?',(trade,)).fetchone()
                command=db.execute('SELECT state FROM commands WHERE trade_id=?',(trade,)).fetchone()
                registry=db.execute('SELECT state,reason FROM execution_lifecycle WHERE trade_id=?',(trade,)).fetchone()
        except (sqlite3.Error,OSError,ApplicationError):
            self._fence('FOUNDATION_UNAVAILABLE')
        fresh=fresh and self.foundation_available and not self.critical
        flat=bool(fresh and terminal and sealed and funded and reservation and reservation[0]=='RELEASED'
                  and command and command[0] in ('COMPLETED','REJECTED_CONFIRMED'))
        phase=engine.status()['phase']
        phase='RECOVERY_REQUIRED' if not funded else 'FLAT_CONFIRMED' if flat else 'PENDING_RECONCILIATION' if not fresh or phase=='FLAT_CONFIRMED' else phase
        direction=p.get('allocation',{}).get('direction')
        early=False;trailing_threshold=None;early_threshold=None
        if funded and p['entry_spx'] is not None and p['peak'] is not None:
            sign=Decimal(1) if direction=='CALL' else Decimal(-1)
            early=not p['ride'] and (Decimal(p['peak'])-Decimal(p['entry_spx']))*sign>=4
            early_threshold=str(Decimal(p['entry_spx'])+sign*Decimal('1.25')) if early else None
            trailing_threshold=str(Decimal(p['peak'])-sign*3) if p['ride'] else None
        protections=[]
        for cid,quantity in positions.items():
            average,stop=engine._premium_stop(cid)
            valid=False
            try:
                quote=engine._assess(cid,'SELL')[0]
                EvidenceClock().validate_option_risk('CALL' if quote.option.contract.right=='C' else 'PUT',quote.option,self.wall_clock(),self.monotonic_clock(),lambda _:True)
                valid=True
            except Exception:pass
            protections.append({'contract_id':cid,'quantity':quantity,'weighted_average_premium':str(average),'initial_premium_stop':str(stop),
                                'exit_quote_healthy':valid,'broker_held':False})
        return {'trade_id':trade,'state':phase,'reservation_state':reservation[0] if reservation else 'UNKNOWN',
            'native_protection_state':self._native_state(engine),
            'exit_execution_health':p.get('adaptive_exit',{}).get('last_condition'),
            'reconciliation_state':registry[0] if registry else 'UNKNOWN','recovery_reason':registry[1] if registry else 'UNKNOWN',
            'reconciled_flat':flat,'direction':direction,'open_contracts':sum(positions.values()),
            'allocation':[{'strike':t['contract']['strike'],'target':t['quantity'],'owned':positions.get(int(cid),0)} for cid,t in p['targets'].items()],
            'pending_orders':sum(not i['terminal_proven'] for i in engine.state['intents'].values()),'entry_blocked':p['halted'] or not fresh or engine._entry_filled() or trade not in self.leases,
            'exit_reason':p['exit_reason'],'emergency':p['emergency'] or self.app.protection.emergency.blocked(),
            'premium_protection':protections,'protection_mode':'OFFLINE_CONDITIONAL_MODEL_NOT_BROKER_HELD',
            'favorable_spx_peak':p['peak'],'trailing_active':p['ride'],'trailing_threshold':trailing_threshold,
            'early_protection_active':early,'early_protection_threshold':early_threshold,'last_spx_source_time':p['last_spx_source'],
            'submission_enabled':False}

    def _status(self):
        state=self.checkpoint.read();trades=list(state['trades'])
        fresh=self._fresh_account()
        return {'mode':'OFFLINE_SYNTHETIC_EXECUTION_LIFECYCLE','origin':'OFFLINE_FIXTURE','real_ibkr_authenticated':False,
            'broker_connected':bool(self.fixture.connected and self.anchor.session),'account_reconciled':fresh,
            'supervision_mode':'BACKGROUND' if self.start_worker else 'MANUAL_OFFLINE_HARNESS',
            'supervisor_running':bool(self.thread and self.thread.is_alive()),
            'protective_condition':self.critical or ('CRITICAL_UNRESOLVED_PROTECTION' if state.get('critical') else state.get('risk_condition') if str(state.get('risk_condition','')).startswith('CRITICAL') else 'CRITICAL_UNRESOLVED_PROTECTION' if self._native_unresolved() else 'DEGRADED_ENTRY_FENCED' if state['fenced'] else 'HEALTHY_OFFLINE'),
            'entry_fenced':state['fenced'] or self._native_unresolved(),'foundation_available':self.foundation_available,
            'submission_enabled':False,'last_error':self.last_error,'trades':[self._trade_status(trade) for trade in trades]}

    def protective_health(self,service_token):
        """Privileged local diagnostics, even when dashboard storage is down."""
        self.app.protection.require(service_token,'OBSERVE_RISK')
        try:
            state=self.checkpoint.read()
            critical=self.critical or state.get('critical')
            risk=state.get('risk_condition','HEALTHY_OFFLINE')
            if not risk.startswith('CRITICAL') and self._native_unresolved():risk='CRITICAL_UNRESOLVED_PROTECTION'
            return dict(condition='CRITICAL_UNRESOLVED_PROTECTION' if critical else risk if risk.startswith('CRITICAL') else 'DEGRADED_ENTRY_FENCED' if state['fenced'] else 'HEALTHY_OFFLINE',
                entry_fenced=state['fenced'] or bool(critical) or self._native_unresolved(),durable_checkpoint_verified=True,
                foundation_available=self.foundation_available,submission_enabled=False,
                supervisor_running=bool(self.thread and self.thread.is_alive()))
        except Exception:
            return dict(condition='CRITICAL_UNRESOLVED_PROTECTION',entry_fenced=True,
                durable_checkpoint_verified=False,submission_enabled=False,supervisor_running=bool(self.thread and self.thread.is_alive()))

    def status(self,session):
        self._ensure_open()
        foundation=self.app.status(session)
        with self.lock:return dict(foundation,lifecycle=self._status())

    def start_supervision(self,service_token,interval=.1):
        self._ensure_open()
        self.app.protection.require(service_token,'RECONCILE');self.app.protection.require(service_token,'OBSERVE_RISK')
        if not isinstance(interval,(int,float)) or not .01<=interval<=1:raise ApplicationError('BOUNDED_SUPERVISION_INTERVAL_REQUIRED')
        if self.thread is not None:raise ApplicationError('SUPERVISION_ALREADY_STARTED')
        def run():
            while not self.stop_event.is_set():
                try:self.supervise(service_token)
                except Exception:
                    self.proof=None;self._fence('SUPERVISION_REQUIRES_RECOVERY',critical=True)
                self.stop_event.wait(interval)
        self.thread=Thread(target=run,name='mortificatio-offline-protection',daemon=True);self.thread.start()

    def close(self):
        if self.closed:return
        if getattr(self,'entry_worker',None) is not None:self.entry_worker.close()
        self.stop_event.set()
        if self.thread is not None:self.thread.join(timeout=5)
        if self.thread is not None and self.thread.is_alive():raise ApplicationError('SUPERVISION_DID_NOT_STOP')
        with self.lock:
            self.leases.clear();self.fixture.connected=False
            for engine in self.engines.values():engine.close()
            self.engines.clear();self.anchor.close();self.app.broker=self.upstream;self.lock_file.close()
            self.closed=True
