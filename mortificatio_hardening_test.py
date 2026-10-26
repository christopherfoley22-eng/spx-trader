"""Private state and bounded HTTPS worker faults. No listeners or network."""
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import ssl
import stat
import tempfile
from threading import BoundedSemaphore
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from executor_dashboard_web import BoundedLocalHTTPSServer
from mortificatio_models import ApplicationError
from mortificatio_store import private_database


class PrivateStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_database_and_existing_sidecars_are_private_without_data_changes(self):
        target=self.root/'state.sqlite'
        for suffix in ('','-wal','-shm'):
            path=Path(str(target)+suffix);path.write_bytes(b'preserved');path.chmod(0o644)
        private_database(target)
        for suffix in ('','-wal','-shm'):
            path=Path(str(target)+suffix)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(path.read_bytes(),b'preserved')

    def test_shared_parent_is_rejected_without_chmod_or_reset(self):
        shared=self.root/'shared';shared.mkdir(mode=0o755)
        with self.assertRaises(ApplicationError):private_database(shared/'state.sqlite')
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode),0o755)
        self.assertFalse((shared/'state.sqlite').exists())

    def test_symlink_state_and_application_parent_are_rejected(self):
        target=self.root/'target';target.write_bytes(b'untouched')
        alias=self.root/'state.sqlite';alias.symlink_to(target)
        with self.assertRaises(ApplicationError):private_database(alias)
        self.assertEqual(target.read_bytes(),b'untouched')
        directory=self.root/'directory';directory.mkdir(mode=0o700)
        parent=self.root/'alias';parent.symlink_to(directory,target_is_directory=True)
        with self.assertRaises(ApplicationError):private_database(parent/'state.sqlite')

    def test_hardlinked_state_and_wal_are_rejected(self):
        target=self.root/'original';target.write_bytes(b'untouched')
        os.link(target,self.root/'state.sqlite')
        with self.assertRaises(ApplicationError):private_database(self.root/'state.sqlite')
        os.link(target,self.root/'other.sqlite-wal')
        with self.assertRaises(ApplicationError):private_database(self.root/'other.sqlite')
        self.assertEqual(target.read_bytes(),b'untouched')

    def test_writable_nonsticky_ancestor_is_rejected(self):
        unsafe=self.root/'unsafe';unsafe.mkdir();unsafe.chmod(0o777)
        child=unsafe/'private';child.mkdir(mode=0o700)
        with self.assertRaises(ApplicationError):private_database(child/'state.sqlite')

    def test_untrusted_ancestor_owner_and_directory_descriptor_swap_are_rejected(self):
        original=Path.lstat
        def lstat(path):
            result=original(path)
            return SimpleNamespace(st_uid=os.geteuid()+1,st_mode=result.st_mode) if path==self.root.parent else result
        with patch.object(Path,'lstat',lstat):
            with self.assertRaises(ApplicationError):private_database(self.root/'state.sqlite')
        original_stat=os.fstat
        def swapped(descriptor):
            result=original_stat(descriptor)
            return SimpleNamespace(st_uid=os.geteuid()+1,st_mode=result.st_mode,st_dev=result.st_dev,st_ino=result.st_ino)
        with patch('os.fstat',side_effect=swapped):
            with self.assertRaises(ApplicationError):private_database(self.root/'state.sqlite')
        self.assertFalse((self.root/'state.sqlite').exists())


class TransportTest(unittest.TestCase):
    def server(self):
        server=BoundedLocalHTTPSServer.__new__(BoundedLocalHTTPSServer)
        server.capacity=BoundedSemaphore(1);server.tls_context=Mock()
        server.finish_request=Mock();server.shutdown_request=Mock()
        return server

    def test_connection_exhaustion_allocates_no_additional_worker(self):
        server=self.server();server.capacity.acquire();request=Mock()
        with patch.object(ThreadingHTTPServer,'process_request') as worker:
            server.process_request(request,('127.0.0.1',1));worker.assert_not_called()
        request.close.assert_called_once()

    def test_tls_handshake_is_bounded_in_worker_and_capacity_released(self):
        server=self.server();server.capacity.acquire();request=Mock()
        server.process_request_thread(request,('127.0.0.1',1))
        request.settimeout.assert_called_with(5)
        wrapped=server.tls_context.wrap_socket.return_value
        wrapped.do_handshake.assert_called_once();wrapped.settimeout.assert_called_with(5)
        server.finish_request.assert_called_once_with(wrapped,('127.0.0.1',1))
        self.assertTrue(server.capacity.acquire(blocking=False))

    def test_bad_tls_never_reaches_http_or_leaks_capacity(self):
        server=self.server();server.capacity.acquire();request=Mock()
        server.tls_context.wrap_socket.return_value.do_handshake.side_effect=ssl.SSLError('injected')
        server.process_request_thread(request,('127.0.0.1',1))
        server.finish_request.assert_not_called();server.shutdown_request.assert_called_once()
        self.assertTrue(server.capacity.acquire(blocking=False))


if __name__=='__main__':unittest.main()
