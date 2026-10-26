"""Independent durable protection inventory/cursors/fence; never entry authority."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from mortificatio_models import ApplicationError
from mortificatio_store import FoundationStore, encode


class ProtectiveCheckpoint:
    def __init__(self,path,binding,root,clock):
        self.root=Path(root)
        self.store=FoundationStore(path,dict(component='PROTECTION_CHECKPOINT_V1',binding=binding,root=str(root)),clock)
        def initialize(db,now):
            db.execute('CREATE TABLE IF NOT EXISTS protective_checkpoint (singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
            if not db.execute('SELECT 1 FROM protective_checkpoint').fetchone():
                self._save(db,dict(initialized=False,trades={},next_order_id=1000,callback_cursor=0,fenced=False,condition=None,critical=False))
        self.store.transact(initialize)

    def _save(self,db,state):
        raw=encode(state)
        prior=db.execute('SELECT payload FROM protective_checkpoint').fetchone()
        before=json.loads(prior[0]) if prior else {}
        self.store.audit(db,'PROTECTIVE_CHECKPOINT',state_hash=hashlib.sha256(raw.encode()).hexdigest())
        db.execute('INSERT OR REPLACE INTO protective_checkpoint VALUES (1,?)',(raw,))
        if state['fenced'] and any(before.get(key)!=state.get(key) for key in ('fenced','condition','critical','risk_condition')):
            risk=state.get('risk_condition') or ''
            self.store.audit(db,'PROTECTIVE_HEALTH_CHANGED',condition=risk or state['condition'],
                critical=state.get('critical',False) or risk.startswith('CRITICAL'))

    def _read(self,db):
        raw=db.execute('SELECT payload FROM protective_checkpoint').fetchone()[0]
        latest=db.execute('SELECT record FROM security_audit WHERE record LIKE ? ORDER BY sequence DESC LIMIT 1',
            ('%"kind":"PROTECTIVE_CHECKPOINT"%',)).fetchone()
        if latest is None or json.loads(latest[0])['details']['state_hash']!=hashlib.sha256(raw.encode()).hexdigest():
            raise ApplicationError('PROTECTION_CHECKPOINT_INTEGRITY_FAILURE',503)
        state=json.loads(raw)
        if encode(state)!=raw:raise ApplicationError('PROTECTION_CHECKPOINT_INTEGRITY_FAILURE',503)
        return state

    def read(self):return self.store.transact(lambda db,now:deepcopy(self._read(db)))

    def update(self,operation):
        def change(db,now):
            state=self._read(db);before=encode(state);result=operation(state)
            if encode(state)!=before:self._save(db,state)
            return result
        return self.store.transact(change)

    def import_inventory(self,rows,next_id,cursor):
        def merge(state):
            if not state['initialized']:
                if type(next_id) is not int or not 1000<=next_id<2**63 or type(cursor) is not int or cursor<0:
                    raise ApplicationError('INVALID_PROTECTION_MIGRATION_COUNTERS')
                state.update(initialized=True,next_order_id=next_id,callback_cursor=cursor)
            for trade,path,bound in rows:
                expected=str(self.root/(trade+'.sqlite'))
                if path!=expected or bound is not None and bound!=expected:
                    raise ApplicationError('UNREVIEWED_EXISTING_JOURNAL')
                if trade in state['trades'] and state['trades'][trade]!=[path,bound]:
                    # The first registration precedes bind_engine; only that
                    # one-way transition may add a matching binding.
                    if state['trades'][trade]!=[path,None] or bound!=path:
                        raise ApplicationError('PROTECTION_CATALOG_CHANGED')
                state['trades'][trade]=[path,bound]
        self.update(merge)

    def fence(self,condition,critical=False):
        self.update(lambda state:state.update(fenced=True,condition=condition,critical=state.get('critical',False) or critical))

    def cursor(self,value):
        def advance(state):
            if type(value) is not int or value<state['callback_cursor']:
                raise ApplicationError('PROTECTION_CURSOR_ROLLBACK')
            state['callback_cursor']=value
        self.update(advance)

    def risk_condition(self,condition):
        def update(state):
            state['risk_condition']=condition
            if condition!='HEALTHY_OFFLINE':state['fenced']=True
        self.update(update)

    def reserve_ids(self,count):
        def reserve(state):
            if type(count) is not int or not 1<=count<=125:raise ApplicationError('INVALID_ORDER_ID_RESERVATION')
            first=state['next_order_id'];state['next_order_id']+=count
            return first
        return self.update(reserve)
