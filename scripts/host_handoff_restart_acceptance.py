#!/usr/bin/env python3
"""Guarded real-Host acceptance for a Gate restart during third-model handoff.

Run only on the admitted AI Core Host checkout, never during consumer traffic.
This is an operator acceptance probe, not a background service or new runner.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MODELS = {
    "granite": ("ai-granite", "/run/ai-core-backend/granite.sock"),
    "qwen3": ("ai-qwen3", "/run/ai-core-backend/qwen3.sock"),
    "coder": ("ai-coder", "/run/ai-core-backend/coder.sock"),
    "ministral": ("ai-ministral", "/run/ai-core-backend/ministral.sock"),
}
OWNER = "ai-core-host-restart-acceptance"
GATE_PROBE = """
import json, os, sys, urllib.request
headers = {'Authorization': 'Bearer ' + os.environ['AI_CORE_API_KEY']}
path, payload, model = sys.argv[1:4]
ports = {'granite': 18181, 'qwen3': 18182, 'ministral': 18183, 'coder': 18184}
data = None if payload == '-' else payload.encode('utf-8')
if data is not None:
    headers['Content-Type'] = 'application/json'
req = urllib.request.Request('http://127.0.0.1:' + str(ports[model]) + path,
                             headers=headers, data=data)
with urllib.request.urlopen(req, timeout=225) as response:
    print(response.read(65536).decode('utf-8'))
"""


def run(argv, timeout=30):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("COMMAND_FAILED:" + argv[0] + " " + argv[1]
                           + ":" + result.stderr[-160:])
    return result.stdout.strip()


def gate(path, payload=None, timeout=240, model='granite'):
    value = '-' if payload is None else json.dumps(payload, separators=(',', ':'))
    raw = run(["docker", "exec", "ai-core-model-gate", "python3", "-c",
               GATE_PROBE, path, value, model], timeout)
    return json.loads(raw)


def get_state():
    return gate("/internal/handoff/status"), gate("/internal/resources/status")


def resource_snapshot():
    h, r = get_state()
    return {
        "resident": h["resident"], "active": h["active"],
        "recovery_pending": h["recovery_pending"], "handoff": h["handoff"],
        "queued": r["queued"],
        "memory_current": int(r["memory"]["memory.current"]),
        "memory_high": int(r["memory"]["memory.high"]),
        "memory_max": int(r["memory"]["memory.max"]),
        "cpu_max": r["cpu_max"], "cpu_stat": r["cpu_stat"],
        "pressure": r["pressure"], "memory_events": r["memory_events"],
    }


def physically_running():
    return {name for name, (container, _) in MODELS.items()
            if run(["docker", "inspect", "-f", "{{.State.Running}}", container])
            == "true"}


BACKEND_PROBE = """
import json, sys
from runtime.model_gate import backend_http_connection
spec = {"backend_socket": sys.argv[1]}
conn = backend_http_connection(spec, timeout=3)
try:
    conn.request("GET", sys.argv[2])
    response = conn.getresponse()
    if response.status != 200:
        raise RuntimeError("BACKEND_STATUS_NOT_READY")
    print(response.read(65536).decode("utf-8"))
finally:
    conn.close()
"""


def backend_get(name, path):
    # Probe the private socket through the existing Gate mount; never
    # contact retired plaintext backend TCP listeners on the Host.
    return run(["docker", "exec", "ai-core-model-gate", "python3", "-c",
                BACKEND_PROBE, MODELS[name][1], path], timeout=9)


def backend_health(name):
    try:
        return json.loads(backend_get(name, "/health")).get("status") == "ok"
    except Exception:
        return False


def backend_idle(name):
    try:
        slots = json.loads(backend_get(name, "/slots"))
        return isinstance(slots, list) and bool(slots) and all(
            isinstance(slot, dict) and slot.get("is_processing") is False
            for slot in slots)
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-source-sha256", required=True)
    args = parser.parse_args()
    out = {"source": args.expected_source_sha256, "step": "PRECHECK"}
    started = []
    ticket = None
    try:
        running_sha = run(["docker", "exec", "ai-core-model-gate",
                           "python3", "-c", "import hashlib;from pathlib import Path;"
                           "print(hashlib.sha256(Path('/app/runtime/model_gate.py')"
                           ".read_bytes()).hexdigest())"])
        source_sha = hashlib.sha256(
            (Path(__file__).resolve().parents[1] / "runtime/model_gate.py").read_bytes()
        ).hexdigest()
        if source_sha != args.expected_source_sha256 or running_sha != source_sha:
            raise RuntimeError("EXACT_SOURCE_MISMATCH")
        router = subprocess.run(["docker", "inspect", "-f",
                                 "{{.State.Running}}", "ai-llama-router"],
                                text=True, capture_output=True)
        if router.returncode == 0 and router.stdout.strip() == "true":
            raise RuntimeError("RETIRED_ROUTER_RUNNING")
        initial = resource_snapshot()
        out["before"] = initial
        if (physically_running() or any(initial["active"].values())
                or initial["handoff"] is not None or initial["recovery_pending"]
                or initial["queued"]):
            raise RuntimeError("NOT_QUIESCENT_NO_MUTATION")
        if (initial["memory_high"] != 34 * 1024**3 or
                initial["memory_max"] != 36 * 1024**3 or
                initial["cpu_max"].split()[0] == "max"):
            raise RuntimeError("PARENT_LIMIT_CHANGED_NO_MUTATION")
        # The private backend Unix sockets are owned and safely reaped by
        # the shared Coordinator. Never bypass it with raw Docker start.
        for name in ("granite", "qwen3"):
            catalogue = gate("/v1/models", model=name)
            requested_id = catalogue["data"][0]["id"]
            answer = gate("/v1/chat/completions", {
                "model": requested_id,
                "messages": [{"role": "user", "content": "Reply OK."}],
                "max_tokens": 1, "temperature": 0, "stream": False,
            }, timeout=240, model=name)
            started.append(name)
            if not answer.get("choices"):
                raise RuntimeError("RESIDENT_PREPARATION_INFERENCE_FAILED")
        deadline = time.monotonic() + 150
        while not all(backend_health(name) for name in started):
            if time.monotonic() >= deadline:
                raise RuntimeError("FIRST_TWO_BACKENDS_NOT_READY")
            time.sleep(3)
        out["two"] = resource_snapshot()
        if set(out["two"]["resident"]) != {"granite", "qwen3"}:
            raise RuntimeError("EXPECTED_TWO_NOT_RUNNING")
        out["step"] = "HANDOFF"
        response = gate("/internal/handoff/begin", {
            "target": "coder", "owner": OWNER, "ttl": 180,
        }, timeout=230)
        ticket = response["ticket"]
        out["three"] = resource_snapshot()
        if (set(out["three"]["resident"]) != {"granite", "qwen3", "coder"}
                or out["three"]["handoff"] is None):
            raise RuntimeError("THIRD_RESIDENT_NOT_CONFIRMED")
        out["step"] = "RESTART"
        run(["docker", "restart", "--time", "10", "ai-core-model-gate"], 70)
        deadline = time.monotonic() + 90
        while True:
            try:
                after_restart = resource_snapshot()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("GATE_RESTART_NOT_HEALTHY")
                time.sleep(2)
        out["after_restart"] = after_restart
        if (set(after_restart["resident"]) != {"granite", "qwen3", "coder"}
                or after_restart["handoff"] is None):
            raise RuntimeError("DURABLE_HANDOFF_LOST_AFTER_RESTART")
        deadline = time.monotonic() + 65
        while True:
            recovered = resource_snapshot()
            if not recovered["recovery_pending"]:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("RECOVERY_STILL_PENDING")
            time.sleep(3)
        out["recovered"] = recovered
        out["step"] = "ABORT"
        gate("/internal/handoff/abort", {
            "owner": OWNER, "ticket": ticket,
        }, timeout=60)
        ticket = None
        out["after_abort"] = resource_snapshot()
        if (set(out["after_abort"]["resident"]) != {"granite", "qwen3"}
                or out["after_abort"]["handoff"] is not None):
            raise RuntimeError("HANDOFF_ABORT_RECLAIM_FAILED")
        out["step"] = "PHYSICAL_HANDOFF_RESTART_PASS"
    except Exception as error:
        out["error"] = type(error).__name__ + ":" + str(error)[:200]
    finally:
        if ticket is not None:
            try:
                h, _ = get_state()
                if not h["recovery_pending"] and not any(h["active"].values()):
                    gate("/internal/handoff/abort", {"owner": OWNER, "ticket": ticket},
                         timeout=60)
                    out["emergency_abort"] = "PASS"
                    ticket = None
            except Exception as error:
                out["emergency_abort"] = type(error).__name__
        try:
            h, r = get_state()
            safe = (h["handoff"] is None and not h["recovery_pending"]
                    and not any(h["active"].values()) and not r["queued"]
                    and all(backend_idle(name) for name in started))
            if safe:
                for name in reversed(started):
                    run(["docker", "stop", "-t", "12", MODELS[name][0]], 35)
                out["cleanup"] = "PASS"
            else:
                out["cleanup"] = "FAIL_CLOSED_BUSY"
            out["final"] = resource_snapshot()
        except Exception as error:
            out["cleanup"] = "ERROR_" + type(error).__name__
        print(json.dumps(out, ensure_ascii=False))
    return 0 if (out.get("step") == "PHYSICAL_HANDOFF_RESTART_PASS"
                 and out.get("cleanup") == "PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
