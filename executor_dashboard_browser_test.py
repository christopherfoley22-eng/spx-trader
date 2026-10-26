"""Actual Chrome virtual-passkey tests against ephemeral localhost HTTPS only.

No IBKR or external requests. The TLS trust exception and virtual authenticator
exist in an isolated TEST browser profile only; product TLS validation is intact.
"""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from threading import Thread
import ipaddress
import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mortificatio_auth import MortificatioSecurity,SecurityPolicy
from mortificatio_models import ApplicationPolicy
from mortificatio_protection import ProtectionAuthority
from executor_application import UnifiedApplication
from executor_allocation_service import AllocationBoundary
from executor_application_test import FixtureBrokerBoundary
from executor_allocation_test import BINDING,allocation_policy
from executor_lifecycle_test import LiveFixtureProvider
from executor_lifecycle import OfflineExecutionLifecycle
from executor_offline_fixture import OfflineBrokerFixture
from executor_paper_broker_test import approval,NOW,ACCOUNT
from executor_paper_types import PaperSessionEvidence
from executor_dashboard_web import DashboardWebApplication,create_https_server

CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
SCREENSHOTS=Path('/private/tmp/mortificatio-phase5-previews')


def test_tls(root):
    key=ec.generate_private_key(ec.SECP256R1());name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost offline test')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
        .sign(key,hashes.SHA256()))
    key_path=root/'test.key';cert_path=root/'test.pem'
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path,key_path);return context


class BrowserTest(unittest.TestCase):
    def scenario(self,direction):
        self.assertTrue(Path(CHROME).exists(),'Chrome is required for the offline browser suite')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);SCREENSHOTS.mkdir(parents=True,exist_ok=True)
            with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
            origin='https://localhost:'+str(port);owner=SimpleNamespace(now=NOW.timestamp(),spx=6000.)
            owner.wall=lambda:datetime.fromtimestamp(owner.now,timezone.utc);owner.mono=lambda:100.+owner.now-NOW.timestamp()
            security=MortificatioSecurity(root/'foundation.sqlite',SecurityPolicy('localhost',origin),ApplicationPolicy(BINDING),lambda:owner.now)
            bootstrap=security.provision_owner_bootstrap();authority=ProtectionAuthority(root/'protection.sqlite',BINDING);token=authority.provision_local_service()
            owner.broker=FixtureBrokerBoundary(lambda:owner.now,BINDING)
            core=UnifiedApplication(security,authority,owner.broker,AllocationBoundary(allocation_policy(),LiveFixtureProvider(owner)))
            source=OfflineBrokerFixture(root/'fixture.sqlite',ACCOUNT,owner.wall,owner.mono,NOW-timedelta(days=1))
            lifecycle=OfflineExecutionLifecycle(core,root/'service',approval(),source,token,owner.wall,owner.mono,start_worker=False)
            lifecycle.verify_broker_session(token,PaperSessionEvidence(ACCOUNT,(ACCOUNT,),'synthetic-paper-approval',str(uuid.uuid4()),owner.wall(),owner.wall()+timedelta(minutes=30),3))
            server=create_https_server(DashboardWebApplication(lifecycle),('127.0.0.1',port),test_tls(root))
            thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            child=subprocess.Popen(['node',str(Path(__file__).with_suffix('.js'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                child.stdin.write(json.dumps(dict(chrome=CHROME,origin=origin,bootstrap=bootstrap,scenario=direction,profile=str(root/'browser-profile'),screenshots=str(SCREENSHOTS)))+'\n');child.stdin.flush()
                done=False;deadline=time.monotonic()+90
                while time.monotonic()<deadline:
                    readable,_,_=select.select([child.stdout],[],[],1)
                    if not readable:
                        if child.poll() is not None:break
                        continue
                    line=child.stdout.readline()
                    if not line:break
                    data=json.loads(line);event=data['event']
                    if event=='done':done=True;break
                    engine=list(lifecycle.engines.values())[-1]
                    if event=='strategy-close':
                        from simulation_evidence import SPXObservation,FeedStatus
                        trade_id=list(lifecycle.engines)[-1]
                        for price in (6004.,6001.25):
                            owner.now=(owner.wall()+timedelta(milliseconds=100)).timestamp()
                            lifecycle.observe_spx(token,trade_id,str(uuid.uuid4()),SPXObservation(price,FeedStatus.LIVE,owner.wall(),owner.mono()))
                    if event in ('fill','close','strategy-close'):
                        side='BUY' if event=='fill' else 'SELL'
                        for item in list(engine.state['intents'].values()):
                            remaining=item['quantity']-engine.filled(item)
                            if item['side']==side and item['order_id'] is not None and remaining:
                                quantity=min(1,remaining) if side=='BUY' else remaining
                                source.fill(item['order_ref'],quantity,'10' if side=='BUY' else '11',owner.wall(),str(uuid.uuid4())+'.01')
                        lifecycle.supervise(token)
                        if event=='strategy-close':owner.now=(owner.wall()+timedelta(milliseconds=100)).timestamp()
                        if event=='close':
                            # Separate the active-trade and later history-review
                            # phases while respecting the unchanged read-rate cap.
                            owner.now=(owner.wall()+timedelta(seconds=61)).timestamp()
                            lifecycle.supervise(token)
                    elif event=='expire':owner.now+=901;lifecycle.supervise(token)
                    else:self.fail('Unknown browser event')
                    child.stdin.write('{"ok":true}\n');child.stdin.flush()
                child.stdin.close();child.wait(timeout=15)
                errors=child.stderr.read()
                self.assertTrue(done,errors);self.assertEqual(child.returncode,0,errors)
                with source.db() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM fixture_orders').fetchone()[0],0 if direction=='recovery' else 20 if direction=='call' else 5)
                self.assertFalse(lifecycle._status()['submission_enabled'])
            finally:
                if child.poll() is None:child.kill();child.wait()
                for stream in (child.stdin,child.stdout,child.stderr):
                    if stream is not None and not stream.closed:stream.close()
                server.shutdown();server.server_close();thread.join(timeout=3);lifecycle.close()

    def test_call_dashboard_history_and_emergency(self):self.scenario('call')
    def test_put_refresh_and_session_expiry(self):self.scenario('put')
    def test_passkey_recovery_backup_enrollment_and_revocation(self):self.scenario('recovery')


if __name__=='__main__':unittest.main()
