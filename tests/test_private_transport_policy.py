"""Validate opt-in private llama backend Compose policy without Host changes."""
import tempfile
from pathlib import Path
import unittest
import yaml
from runtime.model_gate import load_policy, model_specs_from_compose

ROOT = Path(__file__).resolve().parents[1]


class PrivateTransportPolicyTests(unittest.TestCase):
    def test_socket_mode_requires_no_published_tcp(self):
        cfg = yaml.safe_load((ROOT / 'compose.runtime.yml').read_text())
        model = 'coder'
        for name, candidate in cfg['services'].items():
            args = candidate['command']
            args[args.index('--host') + 1] = '/run/ai-core-backend/' + name + '.sock'
            candidate['ports'] = []
            candidate['volumes'].append('ai-core-backend-bus:/run/ai-core-backend')
        svc = cfg['services'][model]
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'compose.yaml'
            file.write_text(yaml.safe_dump(cfg))
            specs = model_specs_from_compose(
                file, load_policy(ROOT / 'runtime/model-policy.yaml'))
            self.assertEqual(specs[model]['backend_socket'],
                             '/run/ai-core-backend/coder.sock')
            svc['command'][svc['command'].index('--host') + 1] = '0.0.0.0'
            svc['ports'] = ['127.0.0.1:18084:8080']
            file.write_text(yaml.safe_dump(cfg))
            with self.assertRaisesRegex(ValueError, 'MIXED_BACKEND_TRANSPORT'):
                model_specs_from_compose(
                    file, load_policy(ROOT / 'runtime/model-policy.yaml'))
            svc['command'][svc['command'].index('--host') + 1] = (
                '/run/ai-core-backend/coder.sock')
            svc['ports'] = []
            svc['ports'] = ['127.0.0.1:18084:8080']
            file.write_text(yaml.safe_dump(cfg))
            with self.assertRaisesRegex(ValueError, 'BACKEND_UNIX_TCP_PUBLISHED'):
                model_specs_from_compose(
                    file, load_policy(ROOT / 'runtime/model-policy.yaml'))

    def test_unix_http_connection_roundtrip(self):
        import socketserver
        import threading
        from runtime.model_gate import backend_http_connection

        class Respond(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(8192)
                self.request.sendall(
                    b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK')

        with tempfile.TemporaryDirectory() as tmp:
            socket_path = str(Path(tmp) / 'model.sock')
            server = socketserver.UnixStreamServer(socket_path, Respond)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = backend_http_connection(
                    {'backend_socket': socket_path}, timeout=3, upstream=True)
                try:
                    connection.request('GET', '/health')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'OK')
                finally:
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_stopped_backend_reaps_only_refused_unix_socket(self):
        import socket
        from unittest.mock import patch
        from runtime.model_gate import ExistingContainerDriver

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'coder.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(path)
            finally:
                server.close()
            self.assertTrue(Path(path).is_socket())
            driver = ExistingContainerDriver(ROOT / 'compose.runtime.yml')
            spec = {'service': 'coder', 'backend_socket': path}
            with patch('runtime.model_gate.BACKEND_UNIX_DIRECTORY', tmp), \\
                    patch.object(driver, 'running', return_value=False) as running, \\
                    patch.object(driver, '_run') as command:
                driver.start(spec)
            self.assertFalse(Path(path).exists())
            self.assertEqual(running.call_count, 2)
            command.assert_called_once_with('start', 'ai-coder')

    def test_live_unix_listener_is_never_removed_or_restarted(self):
        import socket
        from unittest.mock import patch
        from runtime.model_gate import AdmissionError, ExistingContainerDriver

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'coder.sock')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(1)
            try:
                driver = ExistingContainerDriver(ROOT / 'compose.runtime.yml')
                spec = {'service': 'coder', 'backend_socket': path}
                with patch('runtime.model_gate.BACKEND_UNIX_DIRECTORY', tmp), \\
                        patch.object(driver, 'running', return_value=False), \\
                        patch.object(driver, '_run') as command:
                    with self.assertRaisesRegex(
                            AdmissionError, 'BACKEND_UNIX_SOCKET_NOT_STALE'):
                        driver.start(spec)
                self.assertTrue(Path(path).is_socket())
                command.assert_not_called()
            finally:
                listener.close()

    def test_private_socket_cleanup_refuses_regular_file_and_symlink(self):
        from unittest.mock import patch
        from runtime.model_gate import AdmissionError, ExistingContainerDriver

        with tempfile.TemporaryDirectory() as tmp:
            driver = ExistingContainerDriver(ROOT / 'compose.runtime.yml')
            safe_file = Path(tmp) / 'protected.txt'
            safe_file.write_text('keep')
            candidate = Path(tmp) / 'coder.sock'
            for link in (False, True):
                with self.subTest(symlink=link):
                    if candidate.exists() or candidate.is_symlink():
                        candidate.unlink()
                    if link:
                        candidate.symlink_to(safe_file)
                    else:
                        candidate.write_text('keep')
                    spec = {'service': 'coder', 'backend_socket': str(candidate)}
                    with patch('runtime.model_gate.BACKEND_UNIX_DIRECTORY', tmp), \\
                            patch.object(driver, 'running', return_value=False), \\
                            patch.object(driver, '_run') as command:
                        with self.assertRaisesRegex(
                                AdmissionError, 'BACKEND_UNIX_SOCKET_TYPE_UNSAFE'):
                            driver.start(spec)
                    self.assertTrue(candidate.exists())
                    self.assertEqual(safe_file.read_text(), 'keep')
                    command.assert_not_called()

    def test_private_socket_cleanup_refuses_concurrent_container_restart(self):
        import socket
        from unittest.mock import patch
        from runtime.model_gate import AdmissionError, ExistingContainerDriver

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'coder.sock')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.close()
            driver = ExistingContainerDriver(ROOT / 'compose.runtime.yml')
            spec = {'service': 'coder', 'backend_socket': path}
            with patch('runtime.model_gate.BACKEND_UNIX_DIRECTORY', tmp), \\
                    patch.object(driver, 'running', side_effect=[False, True]), \\
                    patch.object(driver, '_run') as command:
                with self.assertRaisesRegex(
                        AdmissionError, 'BACKEND_UNIX_CONTAINER_RESTARTED'):
                    driver.start(spec)
            self.assertTrue(Path(path).is_socket())
            command.assert_not_called()

    def test_gate_unmounted_private_bus_rejected_before_start(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from runtime.model_gate import run_servers, AdmissionError
        spec = {'coder': {'backend_socket':
                          '/run/ai-core-backend/coder.sock'}}
        c = SimpleNamespace(specs=spec)
        with patch('runtime.model_gate.os.path.ismount', return_value=False):
            with self.assertRaisesRegex(AdmissionError,
                                        'BACKEND_UNIX_GATE_VOLUME_NOT_MOUNTED'):
                run_servers(c, 'existing-key-set', '/tmp/unused-state-path')
