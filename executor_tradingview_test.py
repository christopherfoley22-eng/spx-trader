"""TradingView authenticated ingress, durable audit and replay isolation. Offline."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from executor_tradingview import TradingViewInbox, TradingViewError
from executor_tradingview_web import make_handler, make_server

NOW = datetime(2026,10,23,14,30,tzinfo=timezone.utc)
TOKEN = 'OFFLINE_ONLY_SYNTHETIC_RELAY_TOKEN_123456789'
AUTH = 'Bearer '+TOKEN


def payload(event_id='event-1', **changes):
    return dict(version=1,source='TRADINGVIEW',stream_id='spx-1m-close-v1',symbol='SP:SPX',
                event_id=event_id,event_time=NOW.isoformat(),event_time_kind='ALERT_FIRE',price='6000.25',**changes)


class TradingViewInboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path=self.root/'tradingview.sqlite'
        self.now,self.mono=NOW,100.
        self.inbox=self.open()

    def open(self, **kwargs):
        return TradingViewInbox(self.path,TOKEN,wall_clock=lambda:self.now,monotonic_clock=lambda:self.mono,**kwargs)

    def send(self, data=None):
        return self.inbox.ingest(json.dumps(data or payload()).encode(),AUTH)

    def test_auth_before_parse_and_no_unauthenticated_journal_writes(self):
        for auth in (None,'','Bearer bad',True,'Bearer '+TOKEN+'x','é'):
            with self.assertRaises(TradingViewError):
                self.inbox.ingest(b'not JSON',auth)
        self.assertEqual(self.inbox.audit(),[])
        for token in ('short',' '*40,'é'*40):
            with self.assertRaises(TradingViewError):
                TradingViewInbox(self.root/'bad.sqlite',token)

    def test_event_and_receipt_clocks_remain_separate_and_no_exchange_time(self):
        data=payload()
        data['event_time']=(NOW-timedelta(seconds=2)).isoformat()
        data['bar_time']=(NOW-timedelta(minutes=1)).isoformat()
        result=self.send(data)
        self.assertEqual(result['outcome'],'ACCEPTED')
        record=self.inbox.audit()[0]
        self.assertEqual(record['event']['event_time'],data['event_time'])
        self.assertEqual(record['local_receipt_utc'],NOW.isoformat())
        self.assertEqual(record['local_receipt_monotonic'],100.)
        event=self.inbox.replay_events()[0]
        self.assertIsNone(event['exchange_time'])
        self.assertFalse(result['executable'])
        self.assertNotIn(TOKEN,self.path.read_bytes().decode('latin1')+json.dumps(record))

    def test_duplicate_retries_are_durable_even_when_the_original_is_now_stale(self):
        self.send()
        first=self.inbox.audit()[0]
        self.now+=timedelta(hours=1)
        self.mono+=3600
        self.inbox=self.open()
        self.assertEqual(self.send()['outcome'],'DUPLICATE')
        self.assertEqual(self.inbox.audit()[0],first)
        self.assertEqual(len(self.inbox.replay_events()),1)
        self.assertEqual(self.inbox.status()['receipt_counts'],{'ACCEPTED':1,'DUPLICATE':1})

    def test_semantic_duplicate_json_and_utc_normalization(self):
        self.send()
        data=payload()
        data['event_time']='2026-10-23T10:30:00-04:00'
        data['price']=6000.25
        self.assertEqual(self.send(data)['outcome'],'DUPLICATE')

    def test_changed_duplicate_is_audited_disputes_id_and_blocks_replay(self):
        self.send()
        data=payload()
        data['price']='6001'
        self.assertEqual(self.send(data)['outcome'],'CONFLICT')
        with self.assertRaises(TradingViewError):
            self.inbox.replay_events()
        self.assertEqual(self.inbox.audit()[0]['event']['price'],'6000.25')
        self.inbox=self.open()
        with self.assertRaises(TradingViewError):
            self.inbox.replay_events()

    def test_numeric_json_prices_do_not_lose_precision_or_coerce_identity_types(self):
        body=json.dumps(payload()).replace('"6000.25"','6000.250000000000000000000001').encode()
        self.inbox.ingest(body,AUTH)
        record=self.inbox.audit()[0]
        self.assertEqual(record['event']['price'],'6000.250000000000000000000001')
        self.assertEqual(record['received_json'],body.decode())
        changed=body.replace(b'000001',b'000002')
        self.assertEqual(self.inbox.ingest(changed,AUTH)['outcome'],'CONFLICT')
        invalid=json.dumps(payload()).replace('"event-1"','1.25').encode()
        with self.assertRaises(TradingViewError):
            self.inbox.ingest(invalid,AUTH)

    def test_stale_future_late_and_equal_timestamp_quarantine_persist(self):
        self.send()
        for identity,delta,outcome in (('stale',-61,'STALE'),('future',2,'FUTURE'),('late',-.1,'LATE'),('equal',0,'UNORDERED')):
            data=payload(identity)
            data['event_time']=(NOW+timedelta(seconds=delta)).isoformat()
            self.assertEqual(self.send(data)['outcome'],outcome)
        self.inbox=self.open()
        self.assertEqual(len(self.inbox.audit()),5)
        with self.assertRaises(TradingViewError):
            self.inbox.replay_events()

    def test_exact_symbol_stream_payload_prices_and_timezone(self):
        changes=({'symbol':'SPY'},{'symbol':'SP:SPX_DL'},{'symbol':'ES1!'},{'stream_id':'other'},
                 {'source':'IBKR'},{'version':True},{'price':True},{'price':'NaN'},{'price':0},
                 {'price':-1},{'price':'Infinity'},{'event_id':''},{'event_id':'bad\n'},
                 {'event_time':'2026-10-23T14:30:00'},{'event_time':NOW.timestamp()},
                 {'event_time_kind':'EXCHANGE'},{'unexpected':'ignored'},{'exchange_time':NOW.isoformat()},
                 {'bar_time':(NOW+timedelta(minutes=1)).isoformat()})
        for change in changes:
            data=payload()
            data.update(change)
            with self.subTest(change=change),self.assertRaises(TradingViewError):
                self.send(data)
        records=self.inbox.audit()
        self.assertTrue(all(r['outcome']=='INVALID' and r['received_payload'] is None for r in records))

    def test_malformed_duplicate_keys_nonfinite_and_large_bodies_rejected(self):
        for body in (b'[]',b'{',b'\xff',b'{"x":1,"x":2}',b'{"price":NaN}',b'null'):
            with self.assertRaises(TradingViewError):
                self.inbox.ingest(body,AUTH)
        before=len(self.inbox.audit())
        with self.assertRaises(TradingViewError):
            self.inbox.ingest(b'x'*4097,AUTH)
        self.assertEqual(len(self.inbox.audit()),before)

    def test_concurrent_duplicate_delivery_has_one_admission(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.send(),range(12)))
        self.assertEqual(sum(r['outcome']=='ACCEPTED' for r in results),1)
        self.assertEqual(len(self.inbox.replay_events()),1)
        self.assertEqual(len(self.inbox.audit()),12)

    def test_changed_config_and_unrelated_database_never_reused(self):
        self.send()
        with self.assertRaises(TradingViewError):
            self.open(stream_id='another-stream')
        with self.assertRaises(TradingViewError):
            self.open(max_event_age=61)
        path=self.root/'controller.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE controller (value TEXT)')
        with self.assertRaises(TradingViewError):
            TradingViewInbox(path,TOKEN)
        with sqlite3.connect(path) as db:
            self.assertEqual([r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")],['controller'])

    def test_journal_tampering_is_fail_closed(self):
        self.send()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE tv_receipts SET record='{}' WHERE sequence=1")
        with self.assertRaises(TradingViewError):
            self.inbox.audit()
        with self.assertRaises(TradingViewError):
            self.open()

    def test_crash_before_commit_rolls_back_and_after_commit_survives(self):
        script='''
import os,sys,json
from datetime import datetime,timezone
from executor_tradingview import TradingViewInbox
now=datetime(2026,10,23,14,30,tzinfo=timezone.utc)
token="OFFLINE_ONLY_SYNTHETIC_RELAY_TOKEN_123456789"
inbox=TradingViewInbox(sys.argv[1],token,wall_clock=lambda:now,monotonic_clock=lambda:100.)
if sys.argv[2]=="before":
 original=inbox._append
 def die(*args):
  original(*args)
  os._exit(73)
 inbox._append=die
body=json.dumps(dict(version=1,source="TRADINGVIEW",stream_id="spx-1m-close-v1",symbol="SP:SPX",event_id="event-1",event_time=now.isoformat(),event_time_kind="ALERT_FIRE",price="6000.25")).encode()
inbox.ingest(body,"Bearer "+token)
os._exit(73)
'''
        for phase in ('before','after'):
            result=subprocess.run([sys.executable,'-B','-c',script,str(self.path),phase],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,73,result.stderr)
            self.inbox=self.open()
            self.assertEqual(len(self.inbox.audit()),0 if phase=='before' else 1)
        self.assertEqual(self.send()['outcome'],'DUPLICATE')


class TradingViewHTTPTest(unittest.TestCase):
    setUp=TradingViewInboxTest.setUp
    open=TradingViewInboxTest.open

    def request(self, method='POST',path='/webhooks/tradingview',data=None,headers=None,extra=''):
        body=json.dumps(payload() if data is None else data).encode()
        header={'Host':'127.0.0.1:8766','Authorization':AUTH,'Content-Type':'application/json','Content-Length':str(len(body))}
        header.update(headers or {})
        raw=(f'{method} {path} HTTP/1.1\r\n'+''.join(f'{k}: {v}\r\n' for k,v in header.items())+extra+'\r\n').encode()+body
        class Connection:
            def __init__(self):
                self.input,self.output=io.BytesIO(raw),io.BytesIO()
            def settimeout(self,_): pass
            def makefile(self,mode,*_): return self.input if 'r' in mode else self.output
            def sendall(self,data): self.output.write(data)
        connection=Connection()
        make_handler(self.inbox)(connection,('127.0.0.1',1),type('Server',(),{'server_port':8766})())
        head,body=connection.output.getvalue().split(b'\r\n\r\n',1)
        return int(head.split()[1]),json.loads(body)

    def test_authenticated_endpoint_only_and_no_execution_routes(self):
        for path in ('/api/execute','/api/exit-all','/webhooks/tradingview?token='+TOKEN):
            self.assertEqual(self.request(path=path)[0],404)
        self.assertEqual(self.request(headers={'Authorization':'bad'})[0],401)
        self.assertEqual(self.request(headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request()[0],201)
        self.assertEqual(self.request()[0],200)
        data=payload();data['price']='6001'
        self.assertEqual(self.request(data=data)[0],409)
        code,status=self.request('GET','/status')
        self.assertEqual(code,200)
        self.assertFalse(status['ready'])
        self.assertFalse(status['executable'])
        self.assertNotIn(TOKEN,json.dumps(status))

    def test_header_smuggling_size_and_content_type_fail_closed(self):
        for headers,extra in (({'Content-Type':'text/plain'},''),({'Content-Length':'5000'},''),
                              ({'Transfer-Encoding':'chunked'},''),({},'Content-Length: 1\r\n'),
                              ({},'Authorization: bad\r\n')):
            self.assertGreaterEqual(self.request(headers=headers,extra=extra)[0],400)
        self.assertEqual(self.inbox.audit(),[])

    def test_persistence_failure_never_returns_successful_ack(self):
        with patch.object(self.inbox,'ingest',side_effect=sqlite3.OperationalError('disk unavailable')):
            code,body=self.request()
        self.assertEqual(code,503)
        self.assertFalse(body['executable'])

    def test_server_bind_is_loopback_without_opening_a_socket(self):
        with patch('executor_tradingview_web.ThreadingHTTPServer') as server:
            make_server(self.inbox,8766)
            self.assertEqual(server.call_args.args[0],('127.0.0.1',8766))


if __name__=='__main__':
    unittest.main()
