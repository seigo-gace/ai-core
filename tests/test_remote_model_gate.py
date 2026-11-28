import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from runtime.remote_model_gate import (MODEL_ID, make_remote_handler, read_caller_token,
                                      normalize_completion, RemoteGateError)

class RemoteGateTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def upstream(token, body):
            self.calls.append((token, body))
            return {"model": MODEL_ID, "choices": [{"finish_reason": "stop", "message": {
                "content": '{"ok":true}', "reasoning": "not returned"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "cost": 0.001}}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_remote_handler("test-gate-key", "test-caller-only", upstream))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.body = {"model": MODEL_ID, "messages": [{"role":"system","content":"Return JSON"},{"role":"user","content":"Synthetic example"}],
                     "max_tokens":128,"temperature":0,"stream":False,"response_format":{"type":"json_object"}}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, path, body=None, auth=True, external=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if auth: headers["Authorization"] = "Bearer test-gate-key"
        if external: headers["X-AI-Core-Data-Class"] = "SYNTHETIC_QUALIFICATION"
        try:
            conn.request("POST" if body is not None else "GET", path,
                         json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally: conn.close()

    def test_scoped_provider_and_safe_response(self):
        self.assertEqual(self.call("/v1/models")[1]["data"][0]["id"], MODEL_ID)
        status, result = self.call("/v1/chat/completions", self.body)
        self.assertEqual(status, 200)
        self.assertEqual(result["choices"][0]["message"]["content"], '{"ok":true}')
        self.assertNotIn("reasoning", result["choices"][0]["message"])
        self.assertEqual(result["usage"]["cost"], 0.001)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "test-caller-only")
        self.assertEqual(self.calls[0][1]["reasoning"], {"effort": "none"})
        p = self.calls[0][1]["provider"]
        self.assertTrue(p["enforce_distillable_text"])
        self.assertEqual(p["data_collection"], "deny")
        self.assertFalse(p["allow_fallbacks"])
        self.assertEqual(p["max_price"], {"prompt":0.20,"completion":1.0})

    def test_auth_data_class_and_override_rejected_without_call(self):
        self.assertEqual(self.call("/v1/chat/completions", self.body, auth=False)[0], 401)
        self.assertEqual(self.call("/v1/chat/completions", self.body, external=False)[0], 403)
        for update in ({"model":"another"}, {"max_tokens":4097}, {"stream":True},
                       {"provider":{"data_collection":"allow"}}, {"tools":[]}):
            self.assertIn(self.call("/v1/chat/completions", {**self.body, **update})[0], (400,422))
        self.assertEqual(self.calls, [])

    def test_invalid_model_and_partial_completion_are_not_success(self):
        for value in ({"model":"another"}, {"model":MODEL_ID,"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}):
            with self.assertRaises(RemoteGateError):
                normalize_completion(value)

    def test_caller_file_requires_regular_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"token"
            p.write_text("test-private-caller-token-0123456789")
            os.chmod(p,0o600)
            self.assertEqual(read_caller_token(p),"test-private-caller-token-0123456789")
            os.chmod(p,0o640)
            self.assertEqual(read_caller_token(p),"test-private-caller-token-0123456789")
            os.chmod(p,0o644)
            with self.assertRaises(ValueError): read_caller_token(p)

    def test_caller_metadata_exact_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"token"
            p.write_text("test-private-caller-token-0123456789")
            os.chmod(p,0o600)
            info=p.stat()
            expected={"AI_CORE_CALLER_FILE_EXPECTED_UID":str(info.st_uid),
                      "AI_CORE_CALLER_FILE_EXPECTED_GID":str(info.st_gid),
                      "AI_CORE_CALLER_FILE_EXPECTED_MODE":"0600"}
            with patch.dict(os.environ,expected,clear=True):
                self.assertEqual(read_caller_token(p),"test-private-caller-token-0123456789")
                for key,value in (("AI_CORE_CALLER_FILE_EXPECTED_UID",str(info.st_uid+1)),
                                  ("AI_CORE_CALLER_FILE_EXPECTED_GID",str(info.st_gid+1)),
                                  ("AI_CORE_CALLER_FILE_EXPECTED_MODE","0640")):
                    with patch.dict(os.environ,{key:value}):
                        with self.assertRaisesRegex(ValueError,"CALLER_FILE_METADATA_MISMATCH"):
                            read_caller_token(p)

    def test_caller_metadata_partial_or_malformed_expectation_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"token"
            p.write_text("test-private-caller-token-0123456789")
            os.chmod(p,0o600)
            info=p.stat()
            for expected in ({"AI_CORE_CALLER_FILE_EXPECTED_GID":str(info.st_gid)},
                             {"AI_CORE_CALLER_FILE_EXPECTED_UID":str(info.st_uid),
                              "AI_CORE_CALLER_FILE_EXPECTED_GID":"not-a-gid",
                              "AI_CORE_CALLER_FILE_EXPECTED_MODE":"0600"}):
                with patch.dict(os.environ,expected,clear=True):
                    with self.assertRaisesRegex(ValueError,"CALLER_FILE_EXPECTATION_INVALID"):
                        read_caller_token(p)
