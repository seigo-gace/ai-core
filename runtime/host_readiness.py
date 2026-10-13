#!/usr/bin/env python3
"""Read-only Host readback before model lifecycle cutover.

Never stops/starts Docker, never reads credentials, and never implies that
a model is idle merely because no requests were observed at one instant.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from model_gate import (AdmissionError, ExistingContainerDriver,
                        load_policy, model_specs_from_compose,
                        verify_parent_budget)

ROOT = Path(__file__).resolve().parents[1]

def report(compose, policy, driver=None, parent_budget=None):
    """Return bounded, secret-free facts; injectable driver for offline tests."""
    items = model_specs_from_compose(compose, policy)
    budget = (verify_parent_budget(items, policy)
              if parent_budget is None else parent_budget)
    driver = driver or ExistingContainerDriver(compose)
    containers, resident = [], 0
    for model in items:
        spec = items[model]
        detail = {'model': model, 'api_port': spec['backend_port'],
                  'gate_port': spec['gate_port'], 'warm': spec['warm'],
                  'configured_memory_bytes': spec['memory_limit_bytes'],
                  'configured_cpu_cores': spec['cpu_limit']}
        try:
            container = driver._inspect(spec)
            running = container.get('State', {}).get('Running') is True
            detail['running'] = running
            detail['status'] = 'CONTRACT_PASS'
            resident += int(running)
            if running:
                pid = int(container.get('State', {}).get('Pid') or 0)
                if pid > 0:
                    try:
                        paths = Path('/proc') / str(pid) / 'cgroup'
                        cgroup_entries = paths.read_text().splitlines()
                        cgroup = next((line.split('::', 1)[1] for line in cgroup_entries
                                       if line.startswith('0::')), None)
                        if cgroup and cgroup.startswith('/'):
                            root = Path('/sys/fs/cgroup') / cgroup.lstrip('/')
                            for key in ('memory.current', 'memory.swap.current'):
                                file = root / key
                                if file.is_file():
                                    detail[key.replace('.', '_') + '_bytes'] = int(
                                        file.read_text().strip())
                            events = root / 'memory.events'
                            if events.is_file():
                                data = {}
                                for line in events.read_text().splitlines():
                                    fields = line.split()
                                    if len(fields) == 2 and fields[0] in (
                                            'high', 'max', 'oom', 'oom_kill'):
                                        data[fields[0]] = int(fields[1])
                                detail['memory_events_cumulative'] = data
                    except (OSError, ValueError, StopIteration):
                        detail['runtime_cgroup_metrics'] = 'UNAVAILABLE'
        except (AdmissionError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            detail['status'] = 'CONTRACT_FAIL'
            detail['error'] = error.code if isinstance(error, AdmissionError) else type(error).__name__
        containers.append(detail)
    errors = [item['model'] for item in containers if item['status'] != 'CONTRACT_PASS']
    return {
        'schema': 'ai-core/host-readiness/v1',
        'action': 'READ_ONLY',
        'backend_count': len(containers),
        'configured_max_resident': policy['max_resident'],
        'running_backends': resident,
        'parent_budget': budget,
        'models': containers,
        'backend_contract_failures': errors,
        'consumer_exclusivity': 'UNKNOWN',
        'legacy_router_traffic': 'UNKNOWN',
        'admission': 'BLOCKED' if errors or resident > policy['max_resident'] else 'SOURCE_AND_HOST_ONLY',
        'production_cutover_approved': False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--compose', default=str(ROOT / 'compose.runtime.yml'))
    parser.add_argument('--policy', default=str(ROOT / 'runtime/model-policy.yaml'))
    args = parser.parse_args(argv)
    policy = load_policy(args.policy)
    output = report(args.compose, policy)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    if output['backend_contract_failures']:
        print('HOST_READINESS=FAIL|BACKEND_CONTRACT_MISMATCH')
        return 2
    if output['running_backends'] > policy['max_resident']:
        print('HOST_READINESS=BLOCKED|RESIDENT_COUNT_EXCEEDS_POLICY')
        return 0
    print('HOST_READINESS=PASS_READ_ONLY|EXCLUSIVE_TRAFFIC_UNVERIFIED')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AdmissionError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        code = error.code if isinstance(error, AdmissionError) else type(error).__name__
        print('HOST_READINESS=FAIL|' + code)
        sys.exit(2)
