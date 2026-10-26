"""Synthetic local wheels verify locking only; no install, download or migration."""
import hashlib
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
import zipfile
from mortificatio_runtime_audit import REQUIRED,lock_local_wheels,runtime_audit
from pip._vendor.packaging.tags import platform_tags


class RuntimeAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        for name in REQUIRED:self.wheel(name,{'webauthn':'3.0.1','setuptools':'78.1.1'}.get(name,'100.0'))

    def wheel(self,name,version,requires='',python='>=3.9',tag='py3-none-any'):
        normalized=name.replace('-','_');path=self.root/(normalized+'-'+version+'-'+tag+'.whl')
        text='Metadata-Version: 2.1\nName: '+name+'\nVersion: '+version+'\nRequires-Python: '+python+'\n'+requires
        with zipfile.ZipFile(path,'w') as archive:archive.writestr(normalized+'-'+version+'.dist-info/METADATA',text)
        return path

    def test_exact_complete_artifact_hashes_and_deterministic_lock(self):
        first=lock_local_wheels(self.root);self.assertEqual(first,lock_local_wheels(self.root))
        for wheel in self.root.glob('*.whl'):self.assertIn(hashlib.sha256(wheel.read_bytes()).hexdigest(),first)
        self.assertEqual(first.count('--hash=sha256:'),len(REQUIRED))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux wheel compatibility')
    def test_native_linux_cpython313_wheel_is_accepted(self):
        next(self.root.glob('ibapi-*.whl')).unlink()
        wheel=self.wheel('ibapi','100.0',tag='cp313-cp313-'+next(platform_tags()))
        result=lock_local_wheels(self.root,'3.13.18')
        self.assertIn('ibapi==100.0 --hash=sha256:'+hashlib.sha256(wheel.read_bytes()).hexdigest(),result)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux wheel compatibility')
    def test_linux_rejects_foreign_platforms_and_non313_abi(self):
        tags=('cp313-cp313-macosx_11_0_x86_64','cp313-cp313-win_amd64',
              'cp314-cp314-'+next(platform_tags()))
        for tag in tags:
            with self.subTest(tag=tag):
                next(self.root.glob('ibapi-*.whl')).unlink()
                self.wheel('ibapi','100.0',tag=tag)
                with self.assertRaisesRegex(ValueError,r'Wheel incompatible with CPython 3\.13/native platform:'):
                    lock_local_wheels(self.root,'3.13.18')

    def test_old_vulnerable_setuptools_and_old_webauthn_are_rejected(self):
        for name,version in (('setuptools','58.0.4'),('webauthn','2.7.1'),('pip','26.0.1')):
            good=next(self.root.glob(name+'-*.whl'));saved=good.read_bytes();good.unlink()
            bad=self.wheel(name,version)
            with self.assertRaises(ValueError):lock_local_wheels(self.root)
            bad.unlink();good.write_bytes(saved)

    def test_missing_artifacts_duplicate_versions_and_target_rejection(self):
        with self.assertRaises(ValueError):lock_local_wheels(self.root,'3.9.6')
        self.wheel('setuptools','79.0')
        with self.assertRaises(ValueError):lock_local_wheels(self.root)
        (self.root/'setuptools-79.0-py3-none-any.whl').unlink()
        next(self.root.glob('ibapi-*.whl')).unlink()
        with self.assertRaises(ValueError):lock_local_wheels(self.root)

    def test_unsatisfied_dependency_and_python_metadata_are_rejected(self):
        self.wheel('pip','100.0','Requires-Dist: nonexistent>=1\n')
        with self.assertRaises(ValueError):lock_local_wheels(self.root)
        self.wheel('pip','100.0',python='>=3.14')
        with self.assertRaises(ValueError):lock_local_wheels(self.root)

    def test_symlink_artifact_is_rejected_and_metadata_is_not_extracted(self):
        wheel=next(self.root.glob('ibapi-*.whl'));saved=wheel.read_bytes();wheel.unlink()
        outside=self.root/'outside.zip';outside.write_bytes(saved);wheel.symlink_to(outside)
        with self.assertRaises(ValueError):lock_local_wheels(self.root)
        self.assertFalse((self.root/'ibapi-100.0.dist-info').exists())

    def test_runtime_report_never_claims_execution_or_full_vulnerability_clearance(self):
        report=runtime_audit();self.assertFalse(report['execution_enabled'])
        self.assertFalse(report['vulnerability_scan_complete'])
        self.assertIn(report['assessment'],('NO_GO','REQUIRES_FULL_ACCEPTANCE'))

    def test_missing_required_packages_cannot_pass_runtime_preflight(self):
        with patch('mortificatio_runtime_audit.importlib.metadata.distributions',return_value=[]):
            report=runtime_audit()
        self.assertEqual(report['assessment'],'NO_GO')
        for name in REQUIRED:self.assertIn('MISSING_REQUIRED_PACKAGE:'+name,report['issues'])

    def test_known_compromised_ca_runtime_versions_are_blocked(self):
        for old,new in (((3,13,17),(3,13,18)),((3,14,9),(3,14,10))):
            with patch('mortificatio_runtime_audit.sys.version_info',old):
                self.assertIn('PYTHON_SECURITY_PATCH_REQUIRED',runtime_audit()['issues'])
            with patch('mortificatio_runtime_audit.sys.version_info',new):
                self.assertNotIn('PYTHON_SECURITY_PATCH_REQUIRED',runtime_audit()['issues'])


if __name__=='__main__':unittest.main()
