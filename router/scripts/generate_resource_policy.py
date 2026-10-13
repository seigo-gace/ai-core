#!/usr/bin/env python3
import argparse, json, math, re, sys
from pathlib import Path

UNITS={'B':1,'KB':1000,'MB':1000**2,'GB':1000**3,'TB':1000**4,'KIB':1024,'MIB':1024**2,'GIB':1024**3,'TIB':1024**4}

def parse_bytes(s):
    if not s:
        return 0
    left=str(s).split('/')[0].strip()
    m=re.fullmatch(r'([0-9.]+)\s*([KMGTP]?i?B)', left, re.I)
    return 0 if not m else int(float(m.group(1))*UNITS[m.group(2).upper()])

def fail(msg):
    print('RESOURCE_POLICY=FAIL|'+msg)
    sys.exit(1)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--baseline',required=True)
    ap.add_argument('--slice-output',required=True)
    ap.add_argument('--plan-output',required=True)
    a=ap.parse_args()
    b=json.loads(Path(a.baseline).read_text())
    h=b['host']; d=b['docker']; cs=b['containers']
    cpus=int(h['logical_cpus'] or 0); total=int(h['memory_total_bytes'] or 0)
    if cpus < 2:
        fail(f'logical_cpus_too_low={cpus}')
    if total < 2*1024**3:
        fail('host_memory_too_low')
    if d.get('cgroup_version')!='2':
        fail('cgroup_v2_required')
    if d.get('cgroup_driver')!='systemd':
        fail('docker_systemd_cgroup_driver_required')
    if not all(x in h.get('cgroup_controllers',[]) for x in ['cpu','memory','io','pids']):
        fail('required_cgroup_controllers_missing')

    cpu_cores=max(1, math.floor(cpus*0.50))
    cpu_quota_pct=cpu_cores*100
    mem_high=int(total*0.50)
    mem_max=int(total*0.60)
    if mem_max >= total:
        fail('memory ceiling does not preserve host headroom')

    current=sum(parse_bytes((x.get('stats') or {}).get('MemUsage','')) for x in cs.values())
    if current and current > int(mem_high*0.90):
        fail(f'current_ai_memory_too_close_to_high current={current} high={mem_high}')

    high_mib=max(1, mem_high//(1024**2))
    max_mib=max(high_mib+1, mem_max//(1024**2))
    text=(
        '[Slice]\n'
        'CPUAccounting=yes\n'
        'MemoryAccounting=yes\n'
        'IOAccounting=yes\n'
        'TasksAccounting=yes\n'
        f'CPUQuota={cpu_quota_pct}%\n'
        'CPUWeight=50\n'
        'IOWeight=50\n'
        f'MemoryHigh={high_mib}M\n'
        f'MemoryMax={max_mib}M\n'
    )
    so=Path(a.slice_output); so.parent.mkdir(parents=True,exist_ok=True); so.write_text(text)
    plan={
      'host_logical_cpus':cpus,
      'host_memory_bytes':total,
      'ai_cpu_hard_ceiling_cores':cpu_cores,
      'ai_cpu_quota_percent':cpu_quota_pct,
      'ai_cpu_weight':50,
      'ai_io_weight':50,
      'ai_memory_high_bytes':high_mib*1024**2,
      'ai_memory_max_bytes':max_mib*1024**2,
      'non_ai_min_cpu_headroom_cores':cpus-cpu_cores,
      'non_ai_memory_headroom_at_ai_max_bytes':total-max_mib*1024**2,
      'current_ai_mem_bytes_from_docker_stats':current,
      'cgroup_parent':'ai-core.slice',
      'policy_basis':'Measured host baseline; AI <=50% logical CPU, MemoryHigh ~=50%, MemoryMax ~=60%; preserve capacity for coexisting projects.'
    }
    po=Path(a.plan_output); po.parent.mkdir(parents=True,exist_ok=True); po.write_text(json.dumps(plan,indent=2)+'\n')
    print('RESOURCE_POLICY=PASS')
    print(json.dumps(plan,ensure_ascii=False))

if __name__=='__main__':
    main()
