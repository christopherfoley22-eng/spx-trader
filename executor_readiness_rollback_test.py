"""Fault after reducer mutation proves structural rollback preserves full state."""
from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import patch
import executor_exit_readiness_test as fixtures


class RollbackTest(unittest.TestCase):
    for _name in ('setUp','fresh','account','quotes','schedule','own','enable','intents','ready'):
        locals()[_name]=getattr(fixtures.ReadinessTest,_name)

    def fail_insert(self,kind,data):
        e=self.ledger;before=deepcopy(e.state);sequence=e.sequence;head=e.head
        e.db.execute("CREATE TEMP TRIGGER fail_phase9 BEFORE INSERT ON events WHEN NEW.event_id LIKE 'fault:%' BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):e._append('fault:'+str(sequence)+':'+kind,kind,data)
        finally:e.db.execute('DROP TRIGGER fail_phase9')
        self.assertEqual(e.state,before,(kind,data));self.assertEqual((e.sequence,e.head),(sequence,head))
        self.assertEqual(e.db.execute('SELECT sequence,head FROM binding').fetchone(),(sequence,head))

    def test_every_optimized_metadata_write_set_rolls_back_after_actual_sqlite_failure(self):
        self.ready();now=self.fake.wall.isoformat()
        arrivals={'101':dict(price='9.9',floor='9.6',quote_id='new')}
        for kind,data in (
            ('PORT_SPX',dict(peak='6050',ride=True,source=now,reason='LET_IT_RIDE_REVERSAL',at=now,exit_arrivals=arrivals)),
            ('PORT_EXIT',dict(reason='INITIAL_STOP',emergency=True,at=now,exit_arrivals=arrivals)),
            *[('PORT_EXIT_OPT',dict(op=op,**value)) for op,value in (
                ('METRIC',dict(value={'x':1})),('CONDITION',dict(value='CRITICAL_EXIT_UNRESOLVED')),
                ('ARRIVALS',dict(values=arrivals)),('DISPATCH',dict(at=now,count=2)),
                ('CANCEL_PERMIT',dict(intent_id='old',at=now,cycle=1)),
                ('DEPTH',dict(value=dict(contract={'con_id':101},evidence_id='d',sequence=1,source_time=now,received_monotonic=self.fake.mono))),
                ('EXTERNAL',dict(value={'proof':'changed'})),('EXTERNAL_INVALID',{}),('ENABLE_READINESS',{}))]):
            with self.subTest(kind=kind,data=data):self.fail_insert(kind,data)

    def test_plan_and_bind_failures_preserve_quantities_ids_and_pacing(self):
        self.ready();e=self.ledger;original=e._append;captured={}
        def intercept(identity,kind,data):
            if kind=='PORT_PLAN':captured.update(data);raise OSError('capture proposed write')
            return original(identity,kind,data)
        with patch.object(e,'_append',side_effect=intercept):
            with self.assertRaises(OSError):self.schedule()
        self.fail_insert('PORT_PLAN',captured)
        actions=self.intents(self.schedule())
        bindings=[dict(intent_id=a['intent_id'],order_id=2000+n,client_id=3) for n,a in enumerate(actions)]
        self.fail_insert('PORT_DISPATCH',dict(bindings=bindings,at=self.fake.wall.isoformat(),exit_pacing=dict(at=self.fake.wall.isoformat(),count=len(bindings))))
        self.assertTrue(all(e.state['intents'][a['intent_id']]['order_id'] is None for a in actions))

    def test_unrecognized_and_economic_events_keep_full_snapshot_fallback(self):
        self.ready();e=self.ledger
        for kind,data in (('PORT_EXECUTION',{}),('RECONCILED',{}),('PORT_EXIT_OPT',{'op':'FUTURE_OPERATION'})):
            with patch('executor_paper_broker.deepcopy',side_effect=RuntimeError('full snapshot selected')):
                with self.assertRaisesRegex(RuntimeError,'full snapshot selected'):e._rollback_snapshot(kind,data)


if __name__=='__main__':unittest.main()
