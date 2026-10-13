#!/usr/bin/env python3
import json, sys
from dockerutil import run_docker
CONTAINERS=["ai-granite","ai-qwen3","ai-ministral","ai-coder"]
r=run_docker(["inspect",*CONTAINERS],capture_output=True,text=True)
if r.returncode:
    print("DISCOVER_BACKEND_COMPOSE=FAIL",file=sys.stderr); raise SystemExit(1)
items=json.loads(r.stdout)
out=[]
for x in items:
    labels=x.get("Config",{}).get("Labels",{}) or {}
    out.append({
      "container":x.get("Name","").lstrip("/"),
      "service":labels.get("com.docker.compose.service"),
      "working_dir":labels.get("com.docker.compose.project.working_dir"),
      "config_files":labels.get("com.docker.compose.project.config_files"),
      "project":labels.get("com.docker.compose.project"),
      "log_driver":x.get("HostConfig",{}).get("LogConfig",{}).get("Type"),
    })
print(json.dumps({"status":"PASS","backends":out},ensure_ascii=False,indent=2))
