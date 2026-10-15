"""Regression for exceptions after SSE HTTP headers have been committed."""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from types import SimpleNamespace
from urllib import request
from runtime.model_gate import Coordinator, make_handler


class Driver:
    def running(self, spec):
        return True


class BrokenStream:
    status = 200
    def getheader(self, name, default=''):
        return 'text/event-stream' if name.lower() == 'content-type' else default
    def read1(self, size):
        raise ValueError('malformed upstream stream')


class Connection:
    def __init__(self, *args, **kwargs):
        pass
    def request(self, *args, **kwargs):
        pass
    def getresponse(self):
        return BrokenStream()
    def close(self):
        pass


class SSEHeadersRegression(unittest.TestCase):
    def test_stream_exception_does_not_append_second_http_status(self):
        spec = {'model_id': '/models/test.gguf', 'backend_port': 20001,
                'gate_port': 0, 'service': 'test'}
        gate = Coordinator({'test': spec}, Driver(), max_loaded=1, max_active=1)
        server = ThreadingHTTPServer(('127.0.0.1', 0),
                                     make_handler(gate, 'test', 'local-test-secret'))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            payload = json.dumps({'model': spec['model_id'], 'stream': True}).encode()
            req = request.Request(
                f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                data=payload, method='POST',
                headers={'Authorization':'Bearer local-test-secret',
                         'Content-Type':'application/json'})
            with patch('runtime.model_gate.http', SimpleNamespace(client=SimpleNamespace(HTTPConnection=Connection))):
                with request.urlopen(req, timeout=3) as response:
                    body = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b'UPSTREAM_STREAM_INTERRUPTED', body)
                    self.assertNotIn(b'HTTP/1.0 400', body)
                    self.assertNotIn(b'HTTP/1.0 503', body)
            self.assertEqual(gate.active['test'], 0)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
