#!/usr/bin/env python3
import os, shlex, subprocess

def docker_prefix():
    raw=os.environ.get('AI_CORE_DOCKER_PREFIX','').strip()
    if raw:
        return shlex.split(raw)
    for prefix in (['docker'],['sudo','-n','docker']):
        try:
            p=subprocess.run(prefix+['info'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
            if p.returncode==0: return prefix
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    raise RuntimeError('Docker unavailable: run sudo -v or provide AI_CORE_DOCKER_PREFIX')

def run_docker(args, **kwargs):
    return subprocess.run(docker_prefix()+list(args), **kwargs)

def check_output(args, **kwargs):
    return subprocess.check_output(docker_prefix()+list(args), **kwargs)
