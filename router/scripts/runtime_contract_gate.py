#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
def fail(msg): print('RUNTIME_CONTRACT_GATE=FAIL|'+msg); sys.exit(1)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',default=str(ROOT/'config/config.generated.yaml')); a=ap.parse_args()
    contract=json.loads((ROOT/'evidence/llama-swap-v257-contract.json').read_text())
    cfg=yaml.safe_load(Path(a.config).read_text()); compose=yaml.safe_load((ROOT/'docker-compose.llama-swap.yml').read_text()); dockerfile=(ROOT/'Dockerfile.llama-swap').read_text()
    if contract['version'] != 257: fail('contract version drift')
    if contract['linux_amd64_sha256'] not in dockerfile: fail('release SHA not pinned')
    for f in ('validate_flag','listen_flag','watch_config_flag'):
        if contract['verified_features'].get(f) is not True: fail('unverified feature:'+f)
    if cfg.get('models') != {}: fail('router must not own backend lifecycle')
    if set(cfg.get('peers',{})) != {'granite','qwen3','ministral','coder'}: fail('peer set mismatch')
    if cfg.get('selectors'): fail('role selectors forbidden in router core')
    if cfg.get('globalConcurrencyLimit') != 2: fail('global concurrency mismatch')
    if cfg.get('captureBuffer') != 0: fail('capture must stay disabled')
    svc=compose['services']['llama-swap']
    if svc.get('cgroup_parent') != 'ai-core.slice': fail('router not under ai-core.slice')
    if svc.get('cpus') != '0.50' or svc.get('mem_limit') != '256m': fail('router local ceiling mismatch')
    if set(compose.get('services',{})) != {'llama-swap'}: fail('extra service bundled with router')
    print('RUNTIME_CONTRACT_GATE=PASS')
if __name__=='__main__': main()
