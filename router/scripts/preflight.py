#!/usr/bin/env python3
import argparse, sys, urllib.request
from pathlib import Path
import yaml
PORTS={'granite':18081,'qwen3':18082,'ministral':18083,'coder':18084}
def fail(msg): print('PREFLIGHT=FAIL|'+msg); sys.exit(1)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',required=True); ap.add_argument('--check-live',action='store_true'); a=ap.parse_args()
    cfg=yaml.safe_load(Path(a.config).read_text())
    if cfg.get('globalConcurrencyLimit') != 2: fail('globalConcurrencyLimit must be 2')
    if cfg.get('captureBuffer') != 0: fail('captureBuffer must be 0')
    if cfg.get('globalTTL') != 0: fail('globalTTL must be 0')
    if cfg.get('selectors'): fail('router must not contain AI role selectors')
    peers=cfg.get('peers',{})
    if set(peers) != set(PORTS): fail('peer set mismatch')
    for n,p in PORTS.items():
        if peers[n].get('proxy') != f'http://127.0.0.1:{p}': fail(f'{n} proxy mismatch')
        if len(peers[n].get('models') or []) != 1: fail(f'{n} must expose exactly one live model')
    if cfg.get('apiKeys') != ['${env.LLAMA_SWAP_API_KEY}']: fail('API key must be env-only')
    text=Path(a.config).read_text()
    if any(x in text for x in ['openai.com','anthropic.com','groq.com','googleapis.com','sk-']): fail('cloud endpoint or literal secret found')
    if a.check_live:
        for port in PORTS.values():
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=3) as r:
                if r.status != 200: fail(f'backend {port} unhealthy')
    print('PREFLIGHT=PASS')
if __name__=='__main__': main()
