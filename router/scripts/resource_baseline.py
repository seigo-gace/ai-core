#!/usr/bin/env python3
import argparse, json, os, subprocess, sys
from dockerutil import docker_prefix
from pathlib import Path

BACKENDS = ['ai-granite','ai-qwen3','ai-ministral','ai-coder']

def run(cmd):
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()

def mem_total_bytes():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemTotal:'):
            return int(line.split()[1]) * 1024
    raise RuntimeError('MemTotal not found')

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--output', required=True)
    ap.add_argument('--containers', nargs='*', default=BACKENDS)
    a=ap.parse_args()
    if not Path('/sys/fs/cgroup/cgroup.controllers').exists():
        print('RESOURCE_BASELINE=FAIL|cgroup_v2_required'); return 1

    dp=docker_prefix()
    driver,version,logdriver = run(dp+['info','--format','{{.CgroupDriver}}|{{.CgroupVersion}}|{{.LoggingDriver}}']).split('|',2)
    if version != '2':
        print(f'RESOURCE_BASELINE=FAIL|docker_cgroup_version={version}'); return 1

    inspect=json.loads(run(dp+['inspect',*a.containers]))
    found={x['Name'].lstrip('/'):x for x in inspect}
    missing=[n for n in a.containers if n not in found]
    if missing:
        print('RESOURCE_BASELINE=FAIL|missing_containers='+','.join(missing)); return 1

    stats={}
    for line in run(dp+['stats','--no-stream','--format','{{json .}}',*a.containers]).splitlines():
        if line.strip():
            obj=json.loads(line); stats[obj.get('Name') or obj.get('Container')] = obj

    containers={}
    for name in a.containers:
        x=found[name]; hc=x.get('HostConfig',{}); st=x.get('State',{}); cfg=x.get('Config',{})
        containers[name]={
            'running': bool(st.get('Running')),
            'oom_killed': bool(st.get('OOMKilled')),
            'exit_code': st.get('ExitCode'),
            'restart_count': x.get('RestartCount'),
            'image': cfg.get('Image'),
            'network_mode': hc.get('NetworkMode'),
            'cgroup_parent': hc.get('CgroupParent') or '',
            'nano_cpus': int(hc.get('NanoCpus') or 0),
            'cpu_period': int(hc.get('CpuPeriod') or 0),
            'cpu_quota': int(hc.get('CpuQuota') or 0),
            'cpu_shares': int(hc.get('CpuShares') or 0),
            'memory_limit': int(hc.get('Memory') or 0),
            'memory_reservation': int(hc.get('MemoryReservation') or 0),
            'memory_swap': int(hc.get('MemorySwap') or 0),
            'log_driver': (hc.get('LogConfig') or {}).get('Type'),
            'log_options': (hc.get('LogConfig') or {}).get('Config') or {},
            'cmd': cfg.get('Cmd') or [],
            'entrypoint': cfg.get('Entrypoint') or [],
            'compose_project': (cfg.get('Labels') or {}).get('com.docker.compose.project',''),
            'compose_service': (cfg.get('Labels') or {}).get('com.docker.compose.service',''),
            'compose_workdir': (cfg.get('Labels') or {}).get('com.docker.compose.project.working_dir',''),
            'compose_files': (cfg.get('Labels') or {}).get('com.docker.compose.project.config_files',''),
            'stats': stats.get(name,{}),
        }

    out={
        'host':{
            'logical_cpus': os.cpu_count(),
            'memory_total_bytes': mem_total_bytes(),
            'cgroup_v2': True,
            'cgroup_controllers': Path('/sys/fs/cgroup/cgroup.controllers').read_text().split(),
        },
        'docker':{
            'cgroup_driver':driver,
            'cgroup_version':version,
            'default_logging_driver':logdriver,
        },
        'containers':containers,
    }
    Path(a.output).write_text(json.dumps(out,indent=2,ensure_ascii=False)+'\n')
    if not all(v['running'] and not v['oom_killed'] and v['exit_code']==0 for v in containers.values()):
        print('RESOURCE_BASELINE=FAIL|backend_state'); return 1
    print('RESOURCE_BASELINE=PASS')
    print('OUTPUT='+a.output)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
