#!/usr/bin/env python3
"""One real shared-Coordinator concurrency/metrics acceptance, no product changes.

Run only inside the existing Gate after an admitted idle-host preflight.
Never output model response text, tokens, keys or request metadata.
"""
import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.request

KEY = os.environ["AI_CORE_API_KEY"]
ROOT = "http://127.0.0.1:18181"
METRICS_ROOT = "http://127.0.0.1:18184"
HEADERS = {"Authorization": "Bearer " + KEY}


def query(base, path, payload=None, client=None, timeout=240):
    headers = dict(HEADERS)
    if client:
        headers["X-AI-Core-Client"] = client
        headers["X-AI-Core-Wait-Ms"] = "30000"
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def snapshot():
    with query(METRICS_ROOT, "/internal/metrics/status", timeout=10) as r:
        return json.load(r)


def run_one(client, model, stream):
    started = time.monotonic()
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 24,
        "stream": stream,
        "messages": [{"role": "user", "content":
            "Reply with the result of two plus two as one number."}],
    }
    try:
        with query(ROOT, "/v1/chat/completions", payload, client) as r:
            if r.status != 200:
                return {"ok": False, "http": r.status, "client": client}
            if stream:
                done = False
                has_content = False
                for line in r:
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        done = True
                        continue
                    if done:
                        return {"ok": False, "error": "POST_TERMINAL_CONTENT"}
                    try:
                        item = json.loads(data)
                    except (ValueError, UnicodeDecodeError):
                        return {"ok": False, "error": "MALFORMED_SSE"}
                    if any(isinstance(choice, dict) and
                           bool(choice.get("delta", {}).get("content"))
                           for choice in item.get("choices", [])):
                        has_content = True
                ok = done and has_content
                tokens = None
            else:
                data = json.load(r)
                choices = data.get("choices", [])
                ok = bool(choices and choices[0].get("message", {}).get("content"))
                tokens = data.get("usage", {}).get("completion_tokens")
            return {"ok": ok, "client": client, "stream": stream,
                    "elapsed_ms": int((time.monotonic()-started)*1000),
                    "tokens": tokens}
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        return {"ok": False, "client": client, "stream": stream,
                "error_type": type(exc).__name__}


def main():
    with query(ROOT, "/v1/models", timeout=10) as r:
        models = json.load(r)["data"]
    if len(models) != 1:
        raise RuntimeError("MODEL_CATALOGUE_CHANGED")
    before = snapshot()
    if before["persistence"] != "READY":
        raise RuntimeError("METRICS_PERSISTENCE_UNAVAILABLE")
    t = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(run_one, "core-acceptance-a", models[0]["id"], False)
        b = executor.submit(run_one, "core-acceptance-b", models[0]["id"], True)
        results = [a.result(timeout=245), b.result(timeout=245)]
    after = snapshot()
    stats = after["model_metrics"]["granite"]
    passed = (all(r.get("ok") for r in results)
              and after["window_completed"] >= before["window_completed"] + 2
              and stats["success"] >= 2
              and stats["stream_first_token_ms_p50"] is not None
              and stats["latency_ms_p95"] is not None
              and after["persistence"] == "READY")
    print(json.dumps({
        "acceptance": "AI_CORE_ACTUAL_SHARED_ADMISSION_WITH_METRICS",
        "result": "PASS" if passed else "FAIL",
        "clients": len(results),
        "responses": results,
        "elapsed_ms": int((time.monotonic()-t)*1000),
        "samples_before": before["window_completed"],
        "samples_after": after["window_completed"],
        "granite_stats": stats,
        "persistence": after["persistence"],
    }, separators=(",", ":")), flush=True)
    return 0 if passed else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"acceptance": "AI_CORE_ACTUAL_SHARED_ADMISSION_WITH_METRICS",
                          "result": "FAIL", "error_type": type(exc).__name__}))
        raise SystemExit(3)
