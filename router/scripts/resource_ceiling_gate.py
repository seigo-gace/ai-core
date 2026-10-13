#!/usr/bin/env python3
import argparse, json, sys, subprocess
from pathlib import Path
from dockerutil import run_docker

DEFAULT=['ai-granite','ai-qwen3','ai-ministral','ai-coder','ai-llama-router']

def fail(msg):
    print('RESOURCE_CEILING_GATE=FAIL|'+msg)
    sys.exit(1)

def read_int(path):
    s=Path(path).read_text().strip()
    return s if s=='max' else int(s)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--plan',required=True)
    ap.add_argument('--containers',nargs='*',default=DEFAULT)
    a=ap.parse_args()
    plan=json.loads(Path(a.plan).read_text())
    r=subprocess.run(
        ['systemctl','show','ai-core.slice','-p','ControlGroup','--value'],
        capture_output=True,text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        fail('ai-core.slice_controlgroup_missing')
    cg=r.stdout.strip()
    root=Path('/sys/fs/cgroup') / cg.lstrip('/')
    if not root.is_dir():
        fail('ai-core.slice_cgroup_missing:'+str(root))

    cpu_max=(root/'cpu.max').read_text().strip().split()
    expected=int(plan['ai_cpu_quota_percent'])
    if len(cpu_max)!=2 or cpu_max[0]=='max': fail('cpu.max_unlimited')
    quota,period=map(int,cpu_max)
    actual=round(quota/period*100)
    if abs(actual-expected)>1: fail(f'cpu_quota={actual}% expected={expected}%')

    memmax=read_int(root/'memory.max')
    memhigh=read_int(root/'memory.high')
    if memmax=='max' or int(memmax)!=int(plan['ai_memory_max_bytes']): fail(f'memory.max={memmax}')
    if memhigh=='max' or int(memhigh)!=int(plan['ai_memory_high_bytes']): fail(f'memory.high={memhigh}')

    r=run_docker(['inspect',*a.containers],capture_output=True,text=True)
    if r.returncode: fail('docker_inspect_failed')
    by={x['Name'].lstrip('/'):x for x in json.loads(r.stdout)}
    for name in a.containers:
        x=by.get(name)
        if not x: fail('missing_container:'+name)
        if (x.get('HostConfig',{}).get('CgroupParent') or '')!='ai-core.slice': fail(name+':CgroupParent')
        pid=int(x.get('State',{}).get('Pid') or 0)
        if pid<=0 or 'ai-core.slice' not in Path(f'/proc/{pid}/cgroup').read_text(): fail(name+':kernel_cgroup_mismatch')

    events={}
    for line in (root/'memory.events').read_text().splitlines():
        q=line.split()
        if len(q)==2: events[q[0]]=int(q[1])
    if events.get('oom',0)>0 or events.get('oom_kill',0)>0: fail('oom_event_detected')
    print('RESOURCE_CEILING_GATE=PASS')

if __name__=='__main__':
    main()
