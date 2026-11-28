"""DeepSeek text inference through existing Vault and shared AI Core Gate.
No provider API key leaves Vault. No external model owns local residency.
"""
import http.client
import hmac
import json
import math
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler

REMOTE_PORT = 18186
MODEL_ID = "deepseek/deepseek-v4.1-flash-1017"
MAX_INPUT_BYTES = 128 * 1024
MAX_OUTPUT_TOKENS = 4096
MAX_VAULT_BYTES = 4 * 1024 * 1024
PROVIDER_URL = "https://openrouter.ai/api/v1/chat/completions"


class RemoteGateError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def read_caller_token(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        mode = stat.S_IMODE(info.st_mode)
        # The existing Vault runs as UID 1000 while the capability-stripped
        # Gate runs as UID 0. Use only a verified owner-private group read
        # grant for this dedicated caller file, never DAC override/admin token.
        if (not stat.S_ISREG(info.st_mode) or mode not in (0o600, 0o640) or
                (mode == 0o640 and info.st_gid not in set(os.getgroups()) | {os.getegid()})):
            raise ValueError("CALLER_FILE_INVALID")
        token = stream.read(1025).strip()
    if not 24 <= len(token) <= 1024 or "\n" in token or "\r" in token:
        raise ValueError("CALLER_TOKEN_INVALID")
    return token


def validate_completion(body):
    fields = {"model", "messages", "max_tokens", "temperature", "stream", "response_format"}
    if not isinstance(body, dict) or set(body) != fields:
        raise RemoteGateError(400, "REMOTE_REQUEST_FIELDS_INVALID")
    if body["model"] != MODEL_ID:
        raise RemoteGateError(422, "REMOTE_MODEL_IDENTITY_MISMATCH")
    if (type(body["max_tokens"]) is not int or not 1 <= body["max_tokens"] <= MAX_OUTPUT_TOKENS
            or type(body["temperature"]) not in (int, float) or body["temperature"] != 0
            or body["stream"] is not False or body["response_format"] != {"type": "json_object"}):
        raise RemoteGateError(422, "REMOTE_REQUEST_PARAMETERS_INVALID")
    messages = body["messages"]
    if (not isinstance(messages, list) or len(messages) != 2 or
            any(not isinstance(m, dict) or set(m) != {"role", "content"} or
                m["role"] != role or not isinstance(m["content"], str) or not m["content"].strip()
                for m, role in zip(messages, ("system", "user")))):
        raise RemoteGateError(422, "REMOTE_MESSAGES_INVALID")
    # OpenRouter routes only to distillation-compatible providers; do not
    # silently widen provider or price policy or retry ambiguous paid calls.
    return {**body, "provider": {
        "enforce_distillable_text": True, "data_collection": "deny",
        "require_parameters": True, "allow_fallbacks": False,
        "max_price": {"prompt": 0.20, "completion": 1.0},
    }}


def vault_completion(caller_token, body):
    payload = {
        "secret_id": "openrouter-api-key", "consumer": "ai-core",
        "url": PROVIDER_URL, "method": "POST",
        "secret_header": "authorization", "secret_prefix": "Bearer ",
        "headers": {"content-type": "application/json"}, "body": body,
    }
    conn = http.client.HTTPConnection("127.0.0.1", 8791, timeout=25)
    try:
        conn.request("POST", "/internal/v1/actions/http",
                     body=json.dumps(payload).encode("utf-8"),
                     headers={"Authorization": "Bearer " + caller_token,
                              "Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(MAX_VAULT_BYTES + 1)
        if len(raw) > MAX_VAULT_BYTES:
            raise RemoteGateError(502, "REMOTE_VAULT_RESPONSE_TOO_LARGE")
        if response.status != 200:
            raise RemoteGateError(504 if response.status == 504 else 502,
                                  "REMOTE_VAULT_REQUEST_FAILED")
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or not isinstance(envelope.get("provider"), dict):
            raise RemoteGateError(502, "REMOTE_VAULT_RESPONSE_INVALID")
        provider = envelope["provider"]
        status = provider.get("status")
        if status != 200 or provider.get("ok") is not True:
            raise RemoteGateError(429 if status == 429 else 502, "REMOTE_PROVIDER_REQUEST_FAILED")
        return json.loads(provider["body"])
    except (TimeoutError,):
        raise RemoteGateError(504, "REMOTE_PROVIDER_OUTCOME_UNKNOWN") from None
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        raise RemoteGateError(502, "REMOTE_TRANSPORT_OUTCOME_UNKNOWN") from None
    finally:
        conn.close()


def normalize_completion(value):
    if not isinstance(value, dict) or value.get("model") != MODEL_ID:
        raise RemoteGateError(502, "REMOTE_RESPONSE_MODEL_MISMATCH")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise RemoteGateError(502, "REMOTE_RESPONSE_CHOICES_INVALID")
    choice = choices[0]
    message = choice.get("message")
    if (not isinstance(message, dict) or message.get("tool_calls") or
            not isinstance(message.get("content"), str) or not message["content"].strip() or
            choice.get("finish_reason") != "stop"):
        raise RemoteGateError(502, "REMOTE_RESPONSE_INCOMPLETE")
    usage = value.get("usage", {})
    if not isinstance(usage, dict):
        raise RemoteGateError(502, "REMOTE_USAGE_INVALID")
    safe_usage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
        v = usage.get(key)
        if key != "cost":
            if type(v) is int and v >= 0:
                safe_usage[key] = v
        elif type(v) in (int, float) and math.isfinite(v) and v >= 0:
            safe_usage[key] = v
    # Keep real candidate and accounting only. Never relay raw reasoning,
    # provider metadata, error text, credentials, or tool execution.
    return {"model": MODEL_ID, "choices": [{"index": 0, "finish_reason": "stop",
             "message": {"role": "assistant", "content": message["content"]}}],
            "usage": safe_usage}


def make_remote_handler(api_key, caller_token, transport=vault_completion):
    # No additional scheduling authority: one bounded outbound call at a time,
    # fail fast while busy, and let the existing caller own scheduling.
    busy = threading.BoundedSemaphore(1)
    class Handler(BaseHTTPRequestHandler):
        server_version = "AICoreRemoteGate/1"
        def log_message(self, *_):
            return
        def send_json(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def auth(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + api_key):
                raise RemoteGateError(401, "UNAUTHORIZED")
        def do_GET(self):
            try:
                if self.path == "/health":
                    self.send_json(200, {"status": "ok", "gate_model": "deepseek",
                                         "provider_verified": False})
                    return
                self.auth()
                if self.path != "/v1/models":
                    raise RemoteGateError(404, "NOT_FOUND")
                self.send_json(200, {"object": "list", "data": [{"object": "model", "id": MODEL_ID}]})
            except RemoteGateError as error:
                self.send_json(error.status, {"error": {"code": error.code}})
        def do_POST(self):
            try:
                self.auth()
                if self.path != "/v1/chat/completions":
                    raise RemoteGateError(404, "NOT_FOUND")
                if self.headers.get("X-AI-Core-Data-Class") != "SYNTHETIC_QUALIFICATION":
                    raise RemoteGateError(403, "REMOTE_DATA_CLASS_NOT_ADMITTED")
                length = self.headers.get("Content-Length", "")
                if self.headers.get("Transfer-Encoding") or not length.isascii() or not length.isdecimal():
                    raise RemoteGateError(400, "REMOTE_REQUEST_LENGTH_INVALID")
                size = int(length)
                if not 0 < size <= MAX_INPUT_BYTES:
                    raise RemoteGateError(413, "REMOTE_REQUEST_TOO_LARGE")
                try:
                    self.connection.settimeout(8)
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError("SHORT_BODY")
                    body = validate_completion(json.loads(raw))
                except (ValueError, UnicodeError, OSError):
                    raise RemoteGateError(400, "REMOTE_REQUEST_JSON_INVALID") from None
                if not busy.acquire(blocking=False):
                    raise RemoteGateError(429, "REMOTE_BUSY")
                try:
                    result = normalize_completion(transport(caller_token, body))
                finally:
                    busy.release()
                self.send_json(200, result)
            except RemoteGateError as error:
                self.send_json(error.status, {"error": {"code": error.code}})
    return Handler
