#!/usr/bin/env python3
"""Bounded live per-model private-transport inference acceptance.

Invoke inside the existing ai-core-model-gate container:
  docker exec -i ai-core-model-gate python3 - coder < scripts/host_uds_inference_probe.py
No API key, model output, prompt, or request body is printed.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

PORTS = {"granite": 18181, "qwen3": 18182, "ministral": 18183, "coder": 18184}
def emit(**fields):
    print(json.dumps({"probe": "AI_CORE_UDS_REAL_INFERENCE", **fields}, sort_keys=True), flush=True)

def call(url, *, data=None, timeout=12):
    headers = {"Authorization": "Bearer " + os.environ["AI_CORE_API_KEY"]}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in PORTS:
        emit(result="INVALID_MODEL")
        return 2
    model = sys.argv[1]
    base = "http://127.0.0.1:" + str(PORTS[model])
    if len(os.environ.get("AI_CORE_API_KEY", "")) < 16:
        emit(model=model, result="KEY_NOT_AVAILABLE")
        return 3
    before = None
    try:
        _, before = call(base + "/internal/resources/status")
    except (OSError, ValueError, KeyError):
        pass
    started = time.monotonic()
    try:
        status, catalogue = call(base + "/v1/models")
        model_id = catalogue["data"][0]["id"]
        request = {"model": model_id, "messages": [{"role": "user", "content": "Reply OK."}],
                   "max_tokens": 1, "temperature": 0, "stream": False}
        status, answer = call(base + "/v1/chat/completions",
                              data=json.dumps(request).encode("utf-8"), timeout=78)
        choice_ok = bool(isinstance(answer.get("choices"), list) and answer["choices"]
                         and isinstance(answer["choices"][0], dict)
                         and isinstance(answer["choices"][0].get("message"), dict))
        elapsed = round(time.monotonic() - started, 3)
        after = None
        try:
            _, after = call(base + "/internal/resources/status")
        except (OSError, ValueError, KeyError):
            pass
        changes = {}
        try:
            _, handoff_state = call(base + "/internal/handoff/status")
            changes["resident_after"] = handoff_state["resident"]
            changes["resident_count_after"] = len(handoff_state["resident"])
            changes["handoff_active_after"] = handoff_state["handoff"] is not None
            changes["recovery_pending_after"] = handoff_state["recovery_pending"]
        except (OSError, ValueError, KeyError):
            changes["resident_readback"] = "UNAVAILABLE"
        if before and after:
            changes = {
                "memory_before_bytes": int(before["memory"]["memory.current"]),
                "memory_after_bytes": int(after["memory"]["memory.current"]),
                "memory_high_bytes": int(after["memory"]["memory.high"]),
                "memory_max_bytes": int(after["memory"]["memory.max"]),
                "cpu_max": after["cpu_max"],
                "oom_kill_delta": int(after["memory_events"].get("oom_kill", 0)) -
                                  int(before["memory_events"].get("oom_kill", 0)),
                "queue_after": after["queued"],
                "active_after": sum(after["active"].values())}
        emit(model=model, result="PASS" if status == 200 and choice_ok else "FAIL",
             http_status=status, choice_valid=choice_ok, elapsed_s=elapsed,
             usage=answer.get("usage", {}), **changes)
        return 0 if status == 200 and choice_ok else 1
    except urllib.error.HTTPError as error:
        code = "HTTP_ERROR"
        try:
            body = json.load(error)
            code = body.get("error", {}).get("code", code)
        except (ValueError, TypeError, AttributeError):
            pass
        emit(model=model, result="FAIL", http_status=error.code,
             error_code=str(code)[:72],
             elapsed_s=round(time.monotonic() - started, 3))
        return 1
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        emit(model=model, result="FAIL",
             error_type=type(error).__name__,
             elapsed_s=round(time.monotonic() - started, 3))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
