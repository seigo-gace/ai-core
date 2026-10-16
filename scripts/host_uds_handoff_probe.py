#!/usr/bin/env python3
"""Private Unix Gate physical 2->3->2 handoff probe; run inside Gate, no secret output."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

PORT = {"granite":18181,"qwen3":18182,"ministral":18183,"coder":18184}
OWNER = "ai-core-uds-acceptance"

def call(model, path, payload=None, timeout=65):
    headers = {"Authorization": "Bearer " + os.environ["AI_CORE_API_KEY"]}
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request("http://127.0.0.1:" + str(PORT[model]) + path,
                                 data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)

def resource(model):
    obj=call(model,"/internal/resources/status",timeout=8)
    return {"memory":int(obj["memory"]["memory.current"]),
            "high":int(obj["memory"]["memory.high"]),
            "max":int(obj["memory"]["memory.max"]),
            "cpu":obj["cpu_max"],"oom_kill":int(obj["memory_events"].get("oom_kill",0)),
            "active":sum(obj["active"].values()),"queued":obj["queued"]}

def state(model):
    return call(model,"/internal/handoff/status",timeout=10)

def emit(data):
    print(json.dumps({"probe":"AI_CORE_UDS_REAL_HANDOFF",**data},sort_keys=True),flush=True)

def main():
    ticket=None
    old=None
    result={"result":"FAIL","step":"PRECHECK"}
    t=time.monotonic()
    try:
        first=resource("coder")
        before=state("coder")
        original=before["resident"]
        if not(len(original)==2 and not any(before["active"].values())
               and before["handoff"] is None and not before["recovery_pending"]
               and first["queued"]==0):
            result["reason"]="NOT_TWO_IDLE_RESIDENTS"
            result["resident_before"]=original
            return 2
        if first["high"]!=34*1024**3 or first["max"]!=36*1024**3 or first["cpu"]!="600000 100000":
            result["reason"]="PARENT_BUDGET_MISMATCH"
            return 3
        old=original[0]
        other=original[1]
        target=next(x for x in PORT if x not in original)
        result["before"]=original
        result["old"]=old
        result["target"]=target
        result["step"]="HANDOFF_BEGIN"
        begin=call(old,"/internal/handoff/begin",{"target":target,"owner":OWNER,"ttl":100},timeout=70)
        ticket=begin["ticket"]
        during=state(old)
        mid=resource(old)
        result["three"]=during["resident"]
        result["memory_two_bytes"]=first["memory"]
        result["memory_three_bytes"]=mid["memory"]
        if set(during["resident"])!={old,other,target} or during["handoff"] is None or mid["oom_kill"]!=first["oom_kill"]:
            raise RuntimeError("THIRD_MODEL_STATE_INVALID")
        result["step"]="HANDOFF_COMPLETE"
        call(old,"/internal/handoff/complete",{"owner":OWNER,"ticket":ticket},timeout=45)
        ticket=None
        finished=state(other)
        last=resource(other)
        result["after"]=finished["resident"]
        result["memory_after_bytes"]=last["memory"]
        result["oom_kill_delta"]=last["oom_kill"]-first["oom_kill"]
        result["cpu"]=last["cpu"]
        result["memory_max"]=last["max"]
        result["active_after"]=last["active"]
        result["queued_after"]=last["queued"]
        if set(finished["resident"])!={other,target} or finished["handoff"] is not None or finished["recovery_pending"] or last["active"] or last["queued"] or result["oom_kill_delta"]:
            raise RuntimeError("TWO_MODEL_COMPLETION_INVALID")
        result["result"]="PASS"
        result["step"]="DONE"
        return 0
    except (OSError,ValueError,KeyError,IndexError,TypeError,RuntimeError,urllib.error.HTTPError) as error:
        result["reason"]=type(error).__name__
        if isinstance(error,urllib.error.HTTPError):
            result["http_status"]=error.code
        return 1
    finally:
        if ticket is not None and old is not None:
            try:
                call(old,"/internal/handoff/abort",{"owner":OWNER,"ticket":ticket},timeout=35)
                result["emergency_abort"]="PASS"
            except (OSError,ValueError,KeyError,TypeError) as error:
                result["emergency_abort"]="FAIL_"+type(error).__name__
        result["duration_s"]=round(time.monotonic()-t,3)
        emit(result)

if __name__=="__main__":
    sys.exit(main())
