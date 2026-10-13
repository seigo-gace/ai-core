#!/usr/bin/env python3
import argparse, concurrent.futures, json, time, urllib.error, urllib.request, sys
PORTS={'granite':18081,'qwen3':18082,'ministral':18083,'coder':18084}
def fail(msg): print('LLAMA_ROUTER_E2E=FAIL|'+msg); sys.exit(1)
def req(method,url,key=None,obj=None,timeout=30):
    body=None; headers={}
    if key: headers['Authorization']='Bearer '+key
    if obj is not None: body=json.dumps(obj).encode(); headers['Content-Type']='application/json'
    r=urllib.request.Request(url,data=body,headers=headers,method=method)
    try:
        with urllib.request.urlopen(r,timeout=timeout) as x:
            data=x.read(); c=x.headers.get('content-type',''); return x.status,(json.loads(data.decode()) if 'json' in c else data.decode(errors='replace'))
    except urllib.error.HTTPError as e:
        raw=e.read()
        try: out=json.loads(raw.decode())
        except: out=raw.decode(errors='replace')
        return e.code,out
def discover(port):
    st,o=req('GET',f'http://127.0.0.1:{port}/v1/models')
    if st!=200 or len(o.get('data') or [])!=1: fail(f'backend_model_{port}')
    return o['data'][0]['id']
def chat(base,key,model,prompt,max_tokens=32,extra=None,timeout=120):
    obj={'model':model,'messages':[{'role':'user','content':prompt}],'temperature':0,'max_tokens':max_tokens,'stream':False}
    if extra: obj.update(extra)
    return req('POST',base+'/v1/chat/completions',key,obj,timeout)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--api-key',required=True); ap.add_argument('--base',default='http://127.0.0.1:18080'); a=ap.parse_args(); base=a.base.rstrip('/')
    if req('GET',base+'/health')[0]!=200: fail('health')
    if req('GET',base+'/v1/models')[0]!=401: fail('auth_required')
    st,o=req('GET',base+'/v1/models',a.api_key)
    if st!=200: fail('models_auth')
    ids={n:discover(p) for n,p in PORTS.items()}; listed={x.get('id') for x in o.get('data',[]) if x.get('id')}
    route_ids={n:f"{n}/{mid}" for n,mid in ids.items()}
    expected=set(route_ids.values())
    missing=expected-listed
    if missing: fail('models_missing:'+','.join(sorted(missing)))
    for n,mid in route_ids.items():
        tokens=256 if n=='ministral' else 64
        extra={'chat_template_kwargs':{'enable_thinking':False}} if n=='qwen3' else None
        st,out=chat(base,a.api_key,mid,'Reply exactly OK',tokens,extra)
        if st!=200 or not (out.get('choices') or []):
            fail(f'model_{n}_request')
        content=((out['choices'][0].get('message') or {}).get('content') or '').strip()
        if content!='OK':
            fail(f'model_{n}_content')

    q=route_ids['qwen3']
    prompt='Compute 17*19 and give the final answer.'
    for flag in (False,True):
        st,out=chat(base,a.api_key,q,prompt,128,
            {'chat_template_kwargs':{'enable_thinking':flag}})
        if st!=200 or not (out.get('choices') or []):
            fail('qwen_option_passthrough_'+str(flag))

    st,m=req('GET',base+'/metrics',a.api_key)
    if st!=200: fail('metrics')
    print('LLAMA_ROUTER_E2E=PASS'); print(json.dumps({'models':route_ids},ensure_ascii=False))
if __name__=='__main__': main()
