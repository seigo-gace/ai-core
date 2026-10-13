#!/usr/bin/env python3
import argparse, json, sys, urllib.request
from pathlib import Path
try:
    import yaml
except Exception as e:
    print(f'ERROR: PyYAML required: {e}', file=sys.stderr); sys.exit(2)

BACKENDS = [
    ('granite','http://127.0.0.1:18081'),
    ('qwen3','http://127.0.0.1:18082'),
    ('ministral','http://127.0.0.1:18083'),
    ('coder','http://127.0.0.1:18084'),
]

def get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        if r.status != 200: raise RuntimeError(f'{url}: HTTP {r.status}')
        return json.loads(r.read().decode())

def discover(base):
    h=get_json(base+'/health')
    if h.get('status') not in ('ok','OK','ready'): raise RuntimeError(f'{base}: unexpected health {h}')
    m=get_json(base+'/v1/models'); data=m.get('data') or []
    if len(data) != 1 or not data[0].get('id'): raise RuntimeError(f'{base}: expected exactly one model, got {data}')
    return data[0]['id']

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--output',required=True); ap.add_argument('--api-key-env',default='LLAMA_SWAP_API_KEY'); ap.add_argument('--peer-target',choices=('backend','gate'),default='backend'); ap.add_argument('--exclusive-traffic',action='store_true'); a=ap.parse_args()
    if a.peer_target == 'gate' and not a.exclusive_traffic:
        ap.error('GATE_PEERS_REQUIRE_EXCLUSIVE_TRAFFIC')
    ids={name:discover(url) for name,url in BACKENDS}
    targets={name: 'http://127.0.0.1:'+str(18181+i) for i,name in enumerate(('granite','qwen3','ministral','coder'))} if a.peer_target=='gate' else dict(BACKENDS)
    cfg={
      'healthCheckTimeout':120,
      'logLevel':'info','logToStdout':'proxy','metricsMaxInMemory':250,'captureBuffer':0,
      'performance':{'disabled':False,'every':'15s'},
      'sendLoadingState':False,'includeAliasesInList':False,'globalTTL':0,'unloadTimeout':10,
      'globalConcurrencyLimit':2,
      'apiKeys':[f'${{env.{a.api_key_env}}}'],
      'models':{},
      'peers':{
        'granite':{'proxy':targets['granite'],'models':[ids['granite']], 'timeouts':{'responseHeader':180}},
        'qwen3':{'proxy':targets['qwen3'],'models':[ids['qwen3']], 'timeouts':{'responseHeader':180}},
        'ministral':{'proxy':targets['ministral'],'models':[ids['ministral']], 'timeouts':{'responseHeader':180}},
        'coder':{'proxy':targets['coder'],'models':[ids['coder']], 'timeouts':{'responseHeader':180}},
      },
    }
    p=Path(a.output); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(yaml.safe_dump(cfg,sort_keys=False,allow_unicode=True),encoding='utf-8')
    print(json.dumps({'status':'PASS','output':str(p),'model_ids':ids},ensure_ascii=False))
if __name__=='__main__': main()
