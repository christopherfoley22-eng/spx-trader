"""Durable SYNTHETIC broker-side book. No client, socket, or broker order API.

Model bindings and cancellations are simulated observations only. Fills require
explicit fixture quantity/price AND source execution time; none occur implicitly.
"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime,timedelta
from decimal import Decimal
import json
import sqlite3

from executor_paper_types import (ExecutionRecord,CommissionRecord,PositionRecord,PaperReconciliationSnapshot,COMPONENTS,TERMINAL)
from executor_portfolio import PortfolioOrderRecord
from executor_paper_broker import money,utc,integer,text_id
from simulation_evidence import VerifiedOptionContract
from mortificatio_models import ApplicationError
from mortificatio_store import private_database,encode


def order_record(data):
    data=dict(data);data['contract']=VerifiedOptionContract(**data['contract'])
    return PortfolioOrderRecord(**data)


def execution_record(data):
    data=dict(data);data['contract']=VerifiedOptionContract(**data['contract'])
    return ExecutionRecord(**data)


class OfflineBrokerFixture:
    origin='OFFLINE_FIXTURE'

    def __init__(self,path,account,wall_clock,monotonic_clock,history_start):
        self.path=private_database(path);self.account=account
        self.wall_clock,self.monotonic_clock=wall_clock,monotonic_clock
        self.history_start=utc(history_start)
        self.connected=False
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'fixture_meta' not in tables:raise ApplicationError('UNRELATED_FIXTURE_DATABASE')
            db.execute('CREATE TABLE IF NOT EXISTS fixture_meta (account TEXT PRIMARY KEY,cycle INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO fixture_meta VALUES (?,0)',(account,))
            if db.execute('SELECT account FROM fixture_meta').fetchall()!=[(account,)]:raise ApplicationError('FIXTURE_ACCOUNT_MISMATCH')
            db.execute('CREATE TABLE IF NOT EXISTS fixture_orders (order_ref TEXT PRIMARY KEY,trade_id TEXT NOT NULL,record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS fixture_executions (exec_id TEXT PRIMARY KEY,trade_id TEXT NOT NULL,record TEXT NOT NULL,executed_at TEXT NOT NULL,active INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS fixture_callbacks (sequence INTEGER PRIMARY KEY,trade_id TEXT NOT NULL,kind TEXT NOT NULL,record TEXT NOT NULL,executed_at TEXT)')

    @contextmanager
    def db(self):
        db=sqlite3.connect(str(self.path),timeout=2)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:yield db
        finally:db.close()

    def require_connected(self):
        if self.connected is not True:raise ApplicationError('SYNTHETIC_BROKER_DISCONNECTED')

    def _callback(self,db,trade,kind,record,stamp=None):
        db.execute('INSERT INTO fixture_callbacks(trade_id,kind,record,executed_at) VALUES (?,?,?,?)',
                   (trade,kind,encode(asdict(record)),stamp))

    def bindings(self,trade,engine,identities):
        self.require_connected()
        with self.db() as db:
            for identity in identities:
                item=engine.state['intents'][identity]
                if item['order_id'] is None:raise ApplicationError('MODEL_BINDING_REQUIRED')
                old=db.execute('SELECT trade_id,record FROM fixture_orders WHERE order_ref=?',(item['order_ref'],)).fetchone()
                if old:
                    if old[0]!=trade:raise ApplicationError('SIMULATED_ORDER_REF_COLLISION')
                    continue
                collision=[json.loads(r[0]) for r in db.execute('SELECT record FROM fixture_orders')]
                if any((r['client_id'],r['order_id'])==(item['client_id'],item['order_id']) for r in collision):
                    raise ApplicationError('SIMULATED_ORDER_ID_COLLISION')
                record=PortfolioOrderRecord(self.account,item['order_ref'],item['order_id'],item['order_id']+100000,
                    item['client_id'],VerifiedOptionContract(**item['contract']),item['side'],item['quantity'],0,item['quantity'],
                    'Submitted','STP' if item.get('native_managed') else 'LMT',item.get('stop_price'),item['limit_price'])
                db.execute('INSERT INTO fixture_orders VALUES (?,?,?)',(record.order_ref,trade,encode(asdict(record))))
                self._callback(db,trade,'ORDER',record)

    def cancel_observations(self,trade,engine,actions):
        self.require_connected()
        with self.db() as db:
            for action in actions:
                if action['type']!='CANCEL_REQUIRED':continue
                item=engine.state['intents'][action['intent_id']]
                row=db.execute('SELECT trade_id,record FROM fixture_orders WHERE order_ref=?',(item['order_ref'],)).fetchone()
                # An absent order is NEVER synthesized into a cancellation proof.
                if row is None:continue
                if row[0]!=trade:raise ApplicationError('SIMULATED_CANCELLATION_SCOPE_MISMATCH')
                raw=json.loads(row[1])
                if raw['status'] in TERMINAL:continue
                raw['status']='Cancelled';record=order_record(raw)
                db.execute('UPDATE fixture_orders SET record=? WHERE order_ref=?',(encode(raw),item['order_ref']))
                self._callback(db,trade,'ORDER',record)

    def fill(self,order_ref,quantity,price,executed_at,exec_id,fee='0.65',correction_of=None,bust=False):
        """Inject source-stamped broker evidence; may model a late cancelled fill."""
        self.require_connected();stamp=utc(executed_at).isoformat();price=str(money(price,positive=not bust))
        if not text_id(exec_id) or not integer(quantity,0 if bust else 1,25):raise ApplicationError('INVALID_FIXTURE_FILL')
        with self.db() as db:
            row=db.execute('SELECT trade_id,record FROM fixture_orders WHERE order_ref=?',(order_ref,)).fetchone()
            if row is None:raise ApplicationError('UNKNOWN_FIXTURE_ORDER')
            trade,raw=row[0],json.loads(row[1]);record=ExecutionRecord(self.account,exec_id,order_ref,raw['order_id'],raw['perm_id'],raw['client_id'],
                VerifiedOptionContract(**raw['contract']),raw['side'],quantity,price,correction_of,bust)
            old=db.execute('SELECT record,executed_at FROM fixture_executions WHERE exec_id=?',(exec_id,)).fetchone()
            if old:
                if old!=(encode(asdict(record)),stamp):raise ApplicationError('CONFLICTING_FIXTURE_EXECUTION')
                self._callback(db,trade,'EXECUTION',record,stamp)
                return
            if correction_of:
                if db.execute('SELECT active FROM fixture_executions WHERE exec_id=?',(correction_of,)).fetchone()!=(1,):
                    raise ApplicationError('UNKNOWN_FIXTURE_CORRECTION')
                db.execute('UPDATE fixture_executions SET active=0 WHERE exec_id=?',(correction_of,))
            db.execute('INSERT INTO fixture_executions VALUES (?,?,?,?,1)',(exec_id,trade,encode(asdict(record)),stamp))
            self._callback(db,trade,'EXECUTION',record,stamp)
            if fee is not None:
                self._callback(db,trade,'COMMISSION',CommissionRecord('fixture-fee:'+exec_id,exec_id,str(money(fee))))
            filled=sum(json.loads(r[0])['quantity'] for r in db.execute('SELECT record FROM fixture_executions WHERE active=1')
                       if json.loads(r[0])['order_ref']==order_ref and not json.loads(r[0])['bust'])
            raw['filled']=filled;raw['remaining']=raw['quantity']-filled
            raw['status']='Filled' if filled==raw['quantity'] else 'Cancelled' if raw['status'] in {'Cancelled','ApiCancelled'} else 'Submitted'
            db.execute('UPDATE fixture_orders SET record=? WHERE order_ref=?',(encode(raw),order_ref))
            self._callback(db,trade,'ORDER',order_record(raw))

    def commission(self,trade,record):
        self.require_connected()
        if not isinstance(record,CommissionRecord):raise ApplicationError('COMMISSION_RECORD_REQUIRED')
        with self.db() as db:self._callback(db,trade,'COMMISSION',record)

    def callbacks_after(self,cursor):
        with self.db() as db:return db.execute('SELECT sequence,trade_id,kind,record,executed_at FROM fixture_callbacks WHERE sequence>? ORDER BY sequence',(cursor,)).fetchall()

    def snapshot(self,generation):
        self.require_connected()
        with self.db() as db:
            db.execute('UPDATE fixture_meta SET cycle=cycle+1');cycle=db.execute('SELECT cycle FROM fixture_meta').fetchone()[0]
            orders=[order_record(json.loads(row[0])) for row in db.execute('SELECT record FROM fixture_orders ORDER BY order_ref')]
            executions=[(execution_record(json.loads(r[0])),bool(r[1])) for r in db.execute('SELECT record,active FROM fixture_executions')]
        positions={}
        for execution,active in executions:
            if active and not execution.bust:
                contract=execution.contract;positions[contract]=positions.get(contract,0)+execution.quantity*(1 if execution.side=='BUY' else -1)
        return PaperReconciliationSnapshot(generation,cycle,self.account,tuple(PositionRecord(self.account,c,q) for c,q in positions.items() if q),
            tuple(o for o in orders if o.status not in TERMINAL),tuple(o for o in orders if o.status in TERMINAL),
            tuple(e.exec_id for e,_ in executions),tuple((name,self.monotonic_clock()) for name in COMPONENTS),
            self.history_start,COMPONENTS)
