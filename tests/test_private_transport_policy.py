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
