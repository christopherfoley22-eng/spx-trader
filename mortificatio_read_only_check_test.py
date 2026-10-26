"""Launcher safety tests: no socket, credentials, or actual broker observations."""
import contextlib,io,json,os,tempfile,unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
import mortificatio_read_only_check as cli

class LauncherTest(unittest.TestCase):
    def run_cli(self,args,issues):
        output=io.StringIO()
        with patch.object(cli,'runtime_audit',return_value={'issues':issues}),contextlib.redirect_stdout(output):
            result=cli.main(args)
        return result,json.loads(output.getvalue())
    def test_default_does_not_import_or_connect_transport(self):
        with patch.dict('sys.modules',{'ibkr_phase12_read_only':None}):
            code,report=self.run_cli([],[])
        self.assertEqual(code,0);self.assertFalse(report['connection_attempted'])
        self.assertFalse(report['execution_enabled'])
    def test_authorization_cannot_bypass_unsupported_runtime(self):
        with patch.dict('sys.modules',{'ibkr_phase12_read_only':None}):
            code,report=self.run_cli(['--authorize-read-only-connection'],['TLS_BLOCKED'])
        self.assertEqual(code,1);self.assertEqual(report['status'],'RUNTIME_BLOCKED')
        self.assertFalse(report['connection_attempted'])
    def test_private_new_distinct_capture_destinations(self):
        with tempfile.TemporaryDirectory() as root:
            parent=Path(root).resolve();os.chmod(parent,0o700);path=parent/'capture'
            self.assertEqual(cli.private_destination(str(path)),path)
            path.write_text('existing')
            with self.assertRaises(ValueError):cli.private_destination(str(path))
            link=parent/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):cli.private_destination(str(link))
            os.chmod(parent,0o755)
            with self.assertRaises(ValueError):cli.private_destination(str(parent/'other'))

    def test_authorized_fake_capture_writes_private_report_without_stdout_identity(self):
        with tempfile.TemporaryDirectory() as root:
            parent=Path(root).resolve();os.chmod(parent,0o700)
            report=parent/'report.json';capture=parent/'capture.sqlite'
            fake_run=__import__('unittest.mock',fromlist=['Mock']).Mock(return_value={
                'paper_account_matched':True,'session_failure':None,'private_identity':'PRIVATE_FIXTURE'})
            modules={
                'ibkr_paper_read_only_validation':SimpleNamespace(load_config=lambda p:('approval','settings')),
                'executor_broker_lifecycle_capture':SimpleNamespace(BrokerLifecycleValidation=lambda *a,**k:object()),
                'ibkr_phase12_read_only':SimpleNamespace(run_lifecycle_read_only=fake_run)}
            with patch.dict('sys.modules',modules):
                code,result=self.run_cli(['--authorize-read-only-connection','--config',str(parent/'config'),
                    '--capture',str(capture),'--report',str(report)],[])
            self.assertEqual(code,0);self.assertTrue(result['connection_attempted'])
            self.assertNotIn('PRIVATE_FIXTURE',json.dumps(result))
            self.assertEqual(report.stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(report.read_text())['private_identity'],'PRIVATE_FIXTURE')
            self.assertTrue(fake_run.call_args.kwargs['authorized'])

if __name__=='__main__':unittest.main()
