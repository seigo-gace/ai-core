#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path
import yaml
REQUIRED={'ai-granite','ai-qwen3','ai-ministral','ai-coder'}
def fail(msg): print('BACKEND_OVERRIDE_PREP=FAIL|'+msg); raise SystemExit(1)
def split_files(raw):
    vals=[x.strip() for x in str(raw or '').split(',') if x.strip()]
    return [x for x in vals if not x.endswith('/.ai-core/ai-core.override.yml')]
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--discovery-json',required=True); ap.add_argument('--manifest-output',required=True); a=ap.parse_args()
    rows=json.loads(Path(a.discovery_json).read_text()).get('backends') or []; by={r.get('container'):r for r in rows}
    if set(by)!=REQUIRED: fail('container_set_mismatch')
    groups={}
    for cname,row in by.items():
        service,wd,project=row.get('service'),row.get('working_dir'),row.get('project'); files=split_files(row.get('config_files'))
        if not service or not wd or not project or not files: fail('missing_compose_metadata:'+cname)
        for f in files:
            if not Path(f).is_file(): fail('base_compose_missing:'+cname+':'+f)
        groups.setdefault((wd,tuple(files),project),{})[service]=cname
    manifest=[]
    for (wd,files,project),services in groups.items():
        d=Path(wd)/'.ai-core'; d.mkdir(parents=True,exist_ok=True); path=d/'ai-core.override.yml'
        cfg={'services':{service:{'cgroup_parent':'ai-core.slice'} for service in sorted(services)}}
        path.write_text(yaml.safe_dump(cfg,sort_keys=False))
        manifest.append({'project':project,'working_dir':wd,'base_config_files':list(files),'override_file':str(path),'services':sorted(services),'containers':sorted(services.values())})
    out=Path(a.manifest_output); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    print('BACKEND_OVERRIDE_PREP=PASS'); print('MANIFEST='+str(out))
if __name__=='__main__': main()
