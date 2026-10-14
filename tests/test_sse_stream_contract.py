import json
import unittest
from http.server import BaseHTTPRequestHandler
from urllib import request
from unittest.mock import patch
from test_model_gate import GateHttpTests
from runtime import model_gate

class StreamContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture=GateHttpTests('test_unauthorized')
        self.fixture.setUp()
    def tearDown(self):
        self.fixture.tearDown()
    def probe(self,body,limit=None):
        class Backend(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                self.wfile.write(body)
        self.fixture.backend.RequestHandlerClass=Backend
        payload=json.dumps({'model':'/models/coder.gguf','stream':True}).encode()
        req=request.Request(self.fixture.base+'/v1/chat/completions',data=payload,
            headers={'Authorization':'Bearer EXISTING_TEST_KEY_12345'})
        with patch.object(model_gate,'MAX_RESPONSE_BYTES',limit or model_gate.MAX_RESPONSE_BYTES):
            with request.urlopen(req,timeout=3) as response:
                data=response.read()
        self.assertEqual(self.fixture.coord.active['coder'],0)
        return data
    def test_incomplete_reports_error(self):
        self.assertIn(b'UPSTREAM_STREAM_INCOMPLETE',self.probe(b'data: {}'))
    def test_oversize_reports_error(self):
        self.assertIn(b'UPSTREAM_STREAM_TOO_LARGE',self.probe(b'x'*50,20))
    def test_done_marker_has_no_error(self):
        data=self.probe(b'data: [DONE]')
        self.assertNotIn(b'event: error',data)

if __name__=='__main__': unittest.main()
