"""Independent reduction/reconciliation authority. No dashboard session dependency.

Capabilities are provisioned by the local service operator, never by HTTP login.
No capability in this authority can admit or dispatch new entries.
"""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import sqlite3

from executor_emergency import EmergencyLatch
from mortificatio_models import AccountBinding, ApplicationError
from mortificatio_store import digest, secret, private_database


class ProtectionAuthority:
    OPERATIONS=frozenset({'OBSERVE_RISK','RECONCILE','EXIT_ALL'})

    def __init__(self,path,binding):
        if not isinstance(binding,AccountBinding):raise ApplicationError('EXPLICIT_PROTECTION_BINDING_REQUIRED')
        binding.validate()
        self.binding,self.path=binding,private_database(path)
        if self.path.is_symlink():raise ApplicationError('SYMLINK_AUTHORITY_REFUSED')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self._db() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables!={'protection_meta','principals'}:raise ApplicationError('REFUSE_UNRELATED_DATABASE')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS protection_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),account TEXT NOT NULL,environment TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS principals (token_hash TEXT PRIMARY KEY,revoked INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO protection_meta VALUES (1,?,?)',(binding.account,binding.environment))
            if db.execute('SELECT account,environment FROM protection_meta').fetchone()!=(binding.account,binding.environment):
                raise ApplicationError('PROTECTION_ACCOUNT_BINDING_MISMATCH')
        self.path.chmod(0o600)
        emergency_path=private_database(self.path.with_name(self.path.name+'.emergency.sqlite'))
        self.emergency=EmergencyLatch(emergency_path,binding.account)

    @contextmanager
    def _db(self):
        db=sqlite3.connect(str(self.path),timeout=1)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:yield db
        finally:db.close()

    def provision_local_service(self):
        token=secret()
        with self._db() as db:db.execute('INSERT INTO principals VALUES (?,0)',(digest('protective-service',token),))
        return token

    def require(self,token,operation):
        if operation not in self.OPERATIONS:raise ApplicationError('PROTECTION_AUTHORITY_CANNOT_OPEN_EXPOSURE')
        with self._db() as db:
            row=db.execute('SELECT revoked FROM principals WHERE token_hash=?',(digest('protective-service',token),)).fetchone()
        if row!=(0,):raise ApplicationError('PROTECTION_SERVICE_AUTHENTICATION_REQUIRED',401)

    def revoke_local_service(self,token):
        with self._db() as db:db.execute('UPDATE principals SET revoked=1 WHERE token_hash=?',(digest('protective-service',token),))

    def exit_all(self,token,request_id):
        self.require(token,'EXIT_ALL')
        return self.emergency.request(request_id)


class IndependentProtectionService:
    """Narrow adapter for the existing OFFLINE portfolio risk/reconciliation model.

    No entry/sizing/dispatch methods. Risk observation still uses existing
    market validation; liquidation planning is allowed only after an exit latch.
    """
    def __init__(self,authority,engine):
        from executor_portfolio import OfflinePortfolioLedger
        if not isinstance(engine,OfflinePortfolioLedger):raise ApplicationError('OFFLINE_PROTECTION_ENGINE_REQUIRED')
        if authority.binding.account!=engine.approval.account:
            raise ApplicationError('PROTECTION_ENGINE_ACCOUNT_MISMATCH')
        self.authority,self.engine=authority,engine

    def observe_spx(self,token,event_id,spx):
        self.authority.require(token,'OBSERVE_RISK')
        return self.engine.observe_spx(self.engine.state['generation'],event_id,spx)

    def reconcile(self,token,snapshot):
        self.authority.require(token,'RECONCILE')
        return self.engine.reconcile(snapshot)

    def exit_all(self,token,request_id):
        self.authority.exit_all(token,request_id)
        return self.engine.trigger_exit(request_id,'EMERGENCY_EXIT_ALL',True)

    def reduction_actions(self,token):
        self.authority.require(token,'OBSERVE_RISK')
        with self.engine.lock:
            if self.authority.emergency.blocked():
                request_id=self.authority.emergency.status()['request_id']
                self.engine.trigger_exit('protection:'+hashlib.sha256(request_id.encode()).hexdigest(),'EMERGENCY_EXIT_ALL',True)
            # A halted acquisition also needs cancellation planning even when no
            # strategy exit has fired (for example an expired dashboard grant).
            if not self.engine.portfolio['exit_reason'] and not self.engine.portfolio['halted']:
                return [{'type':'MONITOR_PROTECTION','executable':False}]
            actions=self.engine.schedule(self.engine.state['generation'])
            if any(action.get('side')=='BUY' for action in actions):raise ApplicationError('REDUCTION_SCOPE_VIOLATION')
            return actions

    def simulate_reduction_bindings(self,token,intent_ids,first_order_id):
        """Assign fake ledger IDs to exits only; no broker API is available."""
        self.authority.require(token,'OBSERVE_RISK')
        with self.engine.lock:
            if not self.engine.portfolio['exit_reason'] or not intent_ids or any(
                    self.engine.state['intents'][key]['side']!='SELL' for key in intent_ids):
                raise ApplicationError('REDUCTION_SCOPE_VIOLATION')
            return self.engine.dispatch_batch(self.engine.state['generation'],intent_ids,first_order_id=first_order_id)
