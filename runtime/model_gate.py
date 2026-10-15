#!/usr/bin/env python3
"""Local, per-model OpenAI-compatible gates for existing AI Core Compose services.

No Docker commands run at import or validation time. Serving requires an
explicitly confirmed exclusive-traffic cutover; the legacy router may not
continue bypassing model leases when eviction is enabled.
"""
import argparse
import contextlib
import fcntl
import hmac
import http.client
import json
import os
import re
import secrets
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

DEFAULT_POLICY = Path(__file__).with_name('model-policy.yaml')
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class AdmissionError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def load_policy(path):
    policy = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if not isinstance(policy, dict) or policy.get('schema') != 'ai-core/model-policy/v1':
        raise ValueError('INVALID_POLICY_SCHEMA')
    models = policy.get('models')
    if not isinstance(models, dict) or not models:
        raise ValueError('NO_POLICY_MODELS')
    for model, item in models.items():
        if not isinstance(model, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}', model):
            raise ValueError('INVALID_SERVICE_NAME')
        if not isinstance(item, dict) or not isinstance(item.get('warm'), bool):
            raise ValueError('INVALID_MODEL_POLICY:' + model)
        port = item.get('gate_port')
        if type(port) is not int or not (1024 <= port <= 65535):
            raise ValueError('INVALID_GATE_PORT:' + model)
    ports = [item['gate_port'] for item in models.values()]
    if len(set(ports)) != len(ports):
        raise ValueError('DUPLICATE_GATE_PORT')
    max_loaded, max_active = policy.get('max_resident'), policy.get('max_active')
    if not (type(max_loaded) is int and type(max_active) is int
            and 1 <= max_active <= max_loaded <= len(models)):
        raise ValueError('INVALID_ADMISSION_LIMITS')
    idle = policy.get('idle_seconds')
    if type(idle) is not int or not (10 <= idle <= 3600):
        raise ValueError('INVALID_IDLE_SECONDS')
    return policy


def parse_bytes(value):
    match = re.fullmatch(r'([1-9][0-9]*)([kmg])', str(value).lower())
    if not match:
        raise ValueError('INVALID_MEMORY_LIMIT')
    return int(match.group(1)) * {'k':1024, 'm':1024**2, 'g':1024**3}[match.group(2)]


def model_specs_from_compose(path, policy):
    """Derive model IDs, limits and backend ports from existing Compose source."""
    cfg = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    services = cfg.get('services') or {}
    specs = {}
    for model, item in policy['models'].items():
        svc = services.get(model)
        if not isinstance(svc, dict):
            raise ValueError('MISSING_COMPOSE_SERVICE:' + model)
        cmd = svc.get('command') or []
        if not isinstance(cmd, list) or '-m' not in cmd:
            raise ValueError('MISSING_MODEL_ID:' + model)
        idx = cmd.index('-m')
        if idx + 1 >= len(cmd) or not str(cmd[idx + 1]).startswith('/models/'):
            raise ValueError('INVALID_MODEL_ID:' + model)
        ports = svc.get('ports') or []
        if len(ports) != 1:
            raise ValueError('INVALID_BACKEND_PORT:' + model)
        parts = str(ports[0]).split(':')
        if len(parts) != 3 or parts[0] != '127.0.0.1' or not parts[1].isdigit():
            raise ValueError('BACKEND_NOT_LOOPBACK:' + model)
        memory = str(svc.get('mem_limit') or '')
        limit_bytes = parse_bytes(memory)
        try:
            cpus = float(svc.get('cpus') or 0)
        except (TypeError, ValueError):
            raise ValueError('INVALID_CPU_LIMIT:' + model)
        if not (0 < cpus <= 64):
            raise ValueError('INVALID_CPU_LIMIT:' + model)
        if '--cache-ram' not in cmd:
            raise ValueError('PROMPT_CACHE_CONTRACT_MISSING:' + model)
        specs[model] = {'model_id':cmd[idx+1], 'backend_port':int(parts[1]),
                        'gate_port':item['gate_port'], 'service':model,
                        'memory_limit_bytes':limit_bytes, 'cpu_limit':cpus,
                        'warm':item['warm']}
    if len({v['backend_port'] for v in specs.values()}) != len(specs):
        raise ValueError('DUPLICATE_BACKEND_PORT')
    return specs


def verify_parent_budget(specs, policy, *, cgroup_root=Path('/sys/fs/cgroup'),
                         control_group=None):
    """Read current parent cgroup ceilings; never overwrite approved limits."""
    if control_group is None:
        r = subprocess.run(['systemctl','show','ai-core.slice','-p',
                            'ControlGroup','--value'], capture_output=True,
                           text=True, timeout=8, check=False)
        if r.returncode or not r.stdout.strip():
            raise AdmissionError(503, 'PARENT_SLICE_MISSING')
        control_group = r.stdout.strip()
    if not control_group.startswith('/') or '..' in Path(control_group).parts:
        raise AdmissionError(503, 'INVALID_CONTROL_GROUP')
    folder = cgroup_root / control_group.lstrip('/')
    try:
        mem_max = int((folder/'memory.max').read_text().strip())
        mem_high = int((folder/'memory.high').read_text().strip())
        quota, period = (folder/'cpu.max').read_text().strip().split()
        cpu_cores = int(quota)/int(period)
    except (OSError, ValueError, ZeroDivisionError) as error:
        raise AdmissionError(503, 'PARENT_BUDGET_UNAVAILABLE') from error
    if not (mem_max > 0 and 0 < mem_high <= mem_max and cpu_cores > 0):
        raise AdmissionError(503, 'PARENT_BUDGET_INVALID')
    count = policy['max_resident']
    maximum_memory = sum(sorted((x['memory_limit_bytes'] for x in specs.values()),
                                reverse=True)[:count])
    maximum_cpu = sum(sorted((x['cpu_limit'] for x in specs.values()),
                             reverse=True)[:count])
    if maximum_memory > mem_max or maximum_cpu > cpu_cores + 0.01:
        raise AdmissionError(409, 'CONFIGURED_RESIDENTS_EXCEED_PARENT_BUDGET')
    return {'parent_memory_max':mem_max, 'parent_memory_high':mem_high,
            'parent_cpu_cores':cpu_cores, 'selected_memory_ceiling':maximum_memory,
            'selected_cpu_ceiling':maximum_cpu}


class ExistingContainerDriver:
    """Operate only already provisioned, Compose-owned, admitted containers.

    A Compose 'up' could recreate a backend WITHOUT the separately installed
    cgroup_parent override; Docker start/stop preserves existing HostConfig.
    """
    def __init__(self, compose_file, timeout=45):
        self.compose_file = str(Path(compose_file).resolve())
        self.timeout = timeout

    def _run(self, *args):
        result = subprocess.run(['docker', *args], capture_output=True, text=True,
                                timeout=self.timeout, check=False)
        if result.returncode:
            raise AdmissionError(503, 'EXISTING_CONTAINER_COMMAND_FAILED')
        return result.stdout.strip()

    def _inspect(self, spec):
        try:
            records = json.loads(self._run('inspect', 'ai-'+spec['service']))
        except (ValueError, TypeError) as error:
            raise AdmissionError(503, 'INVALID_DOCKER_INSPECT') from error
        if not isinstance(records, list) or len(records) != 1:
            raise AdmissionError(503, 'EXISTING_CONTAINER_MISSING')
        container = records[0]
        if container.get('Name') != '/ai-'+spec['service']:
            raise AdmissionError(409, 'CONTAINER_IDENTITY_MISMATCH')
        hc = container.get('HostConfig') or {}
        labels = (container.get('Config') or {}).get('Labels') or {}
        if labels.get('com.docker.compose.project') != 'ai-core' or labels.get('com.docker.compose.service') != spec['service']:
            raise AdmissionError(409, 'COMPOSE_OWNER_MISMATCH')
        if hc.get('CgroupParent') != 'ai-core.slice':
            raise AdmissionError(409, 'BACKEND_CGROUP_NOT_PROTECTED')
        if int(hc.get('Memory') or 0) != spec['memory_limit_bytes']:
            raise AdmissionError(409, 'BACKEND_MEMORY_LIMIT_MISMATCH')
        if int(hc.get('NanoCpus') or 0) != round(spec['cpu_limit']*1_000_000_000):
            raise AdmissionError(409, 'BACKEND_CPU_LIMIT_MISMATCH')
        ports = (hc.get('PortBindings') or {}).get('8080/tcp') or []
        if (len(ports) != 1 or ports[0].get('HostIp') != '127.0.0.1'
                or str(ports[0].get('HostPort')) != str(spec['backend_port'])):
            raise AdmissionError(409, 'BACKEND_LOOPBACK_PORT_MISMATCH')
        cmd = (container.get('Config') or {}).get('Cmd') or []
        if '-m' not in cmd or cmd.index('-m')+1 >= len(cmd) or cmd[cmd.index('-m')+1] != spec['model_id']:
            raise AdmissionError(409, 'BACKEND_MODEL_ID_MISMATCH')
        return container

    def running(self, spec):
        return self._inspect(spec).get('State',{}).get('Running') is True

    def start(self, spec):
        if not self.running(spec):
            self._run('start', 'ai-'+spec['service'])

    def stop(self, spec):
        if self.running(spec):
            self._run('stop', '--time', '30', 'ai-'+spec['service'])

    def healthy(self, spec):
        try:
            conn = http.client.HTTPConnection('127.0.0.1',spec['backend_port'],timeout=3)
            conn.request('GET','/health')
            response = conn.getresponse()
            response.read(4096)
            conn.close()
            return response.status == 200
        except OSError:
            return False


class Coordinator:
    """All model-specific API listeners share one lease/capacity owner."""
    def __init__(self, specs, driver, max_loaded=2, max_active=2, idle_seconds=120,
                 start_timeout=180, clock=time.monotonic, observe_only=False,
                 handoff_budget_check=None):
        if not (1 <= max_loaded <= len(specs) and 1 <= max_active <= max_loaded):
            raise ValueError('INVALID_ADMISSION_LIMITS')
        self.specs, self.driver = specs, driver
        self.max_loaded, self.max_active = max_loaded, max_active
        self.observe_only = observe_only
        self.transitioning = set()  # cold-loading model; prohibit another cold transition
        self.idle_seconds, self.start_timeout, self.clock = idle_seconds, start_timeout, clock
        self.lock = threading.RLock()
        self.active = {name:0 for name in specs}
        self.used = {name:None for name in specs}
        self.semaphore = threading.BoundedSemaphore(max_active)
        self.handoff = None  # bounded, single temporary third-model slot
        self.handoff_budget_check = handoff_budget_check or self._verify_handoff_budget

    def running(self):
        return {name for name,spec in self.specs.items() if self.driver.running(spec)}

    def preflight(self):
        """Never implicitly kill legacy production processes on gate startup."""
        with self.lock:
            loaded = self.running()
            if len(loaded) > self.max_loaded and not self.observe_only:
                raise AdmissionError(409, 'EXISTING_RESIDENCY_EXCEEDS_POLICY')
            # Inherited ready residents must enter the same idle TTL policy
            # as models first loaded by this coordinator.
            if not self.observe_only:
                started = self.clock()
                for name in loaded:
                    if self.used[name] is None:
                        self.used[name] = started
            return loaded

    def _make_room(self, target):
        loaded = self.running()
        if self.handoff is not None:
            raise AdmissionError(429, 'HANDOFF_IN_PROGRESS')
        evicted = []
        if target in loaded:
            return evicted
        while len(loaded) >= self.max_loaded:
            idle = [m for m in loaded if self.active[m] == 0 and m != target]
            if not idle:
                raise AdmissionError(429, 'ALL_RESIDENT_MODELS_LEASED')
            victim = min(idle,key=lambda m:(self.specs[m]['warm'],self.used[m] or 0,m))
            # The cold target is reserved by acquire(). Reserve the victim too:
            # unrelated ready-model leases must not wait on a slow Docker stop.
            self.transitioning.add(victim)
            try:
                self.lock.release()
                try:
                    self.driver.stop(self.specs[victim])
                    if self.driver.running(self.specs[victim]):
                        raise AdmissionError(503, 'EVICTION_NOT_CONFIRMED')
                finally:
                    self.lock.acquire()
            finally:
                self.transitioning.discard(victim)
            evicted.append(victim)
            loaded.remove(victim)
        return evicted

    def _restore_previous(self, target, previous):
        # Called only while holding self.lock. Roll back even when Docker
        # stopped a resident before returning its stop acknowledgment.
        spec = self.specs[target]
        if self.driver.running(spec):
            self.driver.stop(spec)
        for old in previous:
            if not self.driver.running(self.specs[old]):
                self.driver.start(self.specs[old])
            if not self.driver.running(self.specs[old]):
                raise AdmissionError(503, 'ROLLBACK_RESIDENT_NOT_RUNNING')

    def acquire(self, model):
        if model not in self.specs:
            raise AdmissionError(404, 'UNKNOWN_MODEL')
        if not self.semaphore.acquire(blocking=False):
            raise AdmissionError(429, 'GLOBAL_CONCURRENCY_LIMIT')
        try:
            spec = self.specs[model]
            previous = None
            with self.lock:
                if self.active[model]:
                    raise AdmissionError(429, 'MODEL_CONCURRENCY_LIMIT')
                if model in self.transitioning:
                    raise AdmissionError(429, 'MODEL_TRANSITION_BUSY')
                if self.driver.running(spec):
                    self.active[model] += 1
                    self.used[model] = self.clock()
                    return model
                if self.observe_only:
                    raise AdmissionError(503, 'OBSERVE_ONLY_MODEL_NOT_RUNNING')
                if self.transitioning or self.handoff is not None:
                    raise AdmissionError(429, 'MODEL_TRANSITION_BUSY')
                # Reserve the one permitted cold transition, then release the
                # global mutex during slow Docker start/model readiness.
                previous = self.running()
                self.transitioning.add(model)
            try:
                with self.lock:
                    self._make_room(model)
                self.driver.start(spec)
                deadline = self.clock() + self.start_timeout
                while self.clock() < deadline:
                    if self.driver.healthy(spec):
                        break
                    time.sleep(0.4)
                else:
                    raise AdmissionError(503, 'MODEL_START_TIMEOUT')
                with self.lock:
                    self.active[model] += 1
                    self.used[model] = self.clock()
                return model
            except BaseException:
                with self.lock:
                    try:
                        self._restore_previous(model, previous)
                    except Exception as error:
                        raise AdmissionError(503, 'MODEL_START_ROLLBACK_FAILED') from error
                raise
            finally:
                with self.lock:
                    self.transitioning.discard(model)
        except BaseException:
            self.semaphore.release()
            raise

    def release(self, model):
        with self.lock:
            if self.active[model] <= 0:
                raise RuntimeError('DOUBLE_RELEASE')
            self.active[model] -= 1
            self.used[model] = self.clock()
        self.semaphore.release()

    @contextlib.contextmanager
    def lease(self, model):
        self.acquire(model)
        try:
            yield self.specs[model]
        finally:
            self.release(model)

    def _verify_handoff_budget(self):
        # Parent CPU quota governs active inference, not the number of
        # idle-but-resident models during a short handoff.
        budget = verify_parent_budget(
            self.specs, {'max_resident': self.max_active})
        projected = sum(sorted(
            (item['memory_limit_bytes'] for item in self.specs.values()),
            reverse=True)[:3])
        if projected > budget['parent_memory_max']:
            raise AdmissionError(409, 'HANDOFF_MEMORY_CEILING_EXCEEDED')
        # A theoretical 3-container ceiling is insufficient when other
        # work/cache already occupies the parent. Read live usage before
        # starting the third backend; keep 1 GiB below the established High.
        r = subprocess.run(['systemctl', 'show', 'ai-core.slice',
                            '-p', 'ControlGroup', '--value'],
                           capture_output=True, text=True, timeout=8)
        group = r.stdout.strip()
        if r.returncode or not group.startswith('/') or '..' in Path(group).parts:
            raise AdmissionError(503, 'HANDOFF_CGROUP_UNAVAILABLE')
        try:
            current = int((Path('/sys/fs/cgroup') / group.lstrip('/')
                           / 'memory.current').read_text().strip())
        except (OSError, ValueError) as error:
            raise AdmissionError(503, 'HANDOFF_MEMORY_CURRENT_UNAVAILABLE') from error
        # A newly started backend can use its entire allowed container
        # memory even while its initial model weights are loading.
        largest = max(item['memory_limit_bytes'] for item in self.specs.values())
        if current + largest + 1024**3 > budget['parent_memory_high']:
            raise AdmissionError(429, 'HANDOFF_INSUFFICIENT_LIVE_HEADROOM')
        return {**budget, 'selected_memory_ceiling': projected,
                'parent_memory_current': current}

    def handoff_begin(self, previous, target, owner, ttl=120):
        """Reserve one temporary third resident; no HTTP/public API implied.

        The existing ready model remains usable. A handoff reservation is
        explicitly owner-bound and short-lived; a second transition is denied.
        """
        if previous not in self.specs or target not in self.specs or previous == target:
            raise AdmissionError(422, 'INVALID_HANDOFF_MODELS')
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise AdmissionError(422, 'INVALID_HANDOFF_OWNER')
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1 <= ttl <= 600:
            raise AdmissionError(422, 'INVALID_HANDOFF_TTL')
        with self.lock:
            if self.observe_only:
                raise AdmissionError(409, 'OBSERVE_ONLY_NO_HANDOFF')
            if self.handoff or self.transitioning:
                raise AdmissionError(429, 'HANDOFF_IN_PROGRESS')
            loaded = self.running()
            if len(loaded) > self.max_loaded:
                raise AdmissionError(409, 'HANDOFF_RESIDENCY_NOT_READY')
            needs_start = target not in loaded
            if len(loaded) + int(needs_start) > 3:
                raise AdmissionError(409, 'HANDOFF_CAPACITY_EXCEEDED')
            # Reserve under the mutex; the potentially slow parent cgroup
            # probe happens after unlocking so ready inference can proceed.
            self.handoff = {'from': previous, 'to': target, 'owner': owner,
                            'ticket': secrets.token_urlsafe(32),
                            'expires': None, 'ready_ttl': ttl,
                             'started_target': needs_start}
            if needs_start:
                self.transitioning.add(target)
        try:
            budget = self.handoff_budget_check() if needs_start else None
            if needs_start:
                if budget['selected_memory_ceiling'] <= 0 or budget['selected_cpu_ceiling'] <= 0:
                    raise AdmissionError(409, 'HANDOFF_PARENT_BUDGET_INVALID')
                self.driver.start(self.specs[target])
                deadline = self.clock() + self.start_timeout
                while self.clock() < deadline:
                    if self.driver.healthy(self.specs[target]):
                        break
                    time.sleep(0.4)
                else:
                    raise AdmissionError(503, 'HANDOFF_TARGET_NOT_READY')
            with self.lock:
                if len(self.running()) > 3:
                    raise AdmissionError(503, 'HANDOFF_CAPACITY_EXCEEDED')
                self.handoff['expires'] = self.clock() + self.handoff.pop('ready_ttl')
                return dict(self.handoff)
        except BaseException:
            # A failed start can already have made the target resident.
            # Never forget its reservation until its shutdown is confirmed:
            # otherwise a later cold start could create a fourth resident.
            cleanup_ok = False
            try:
                if needs_start and self.driver.running(self.specs[target]):
                    self.driver.stop(self.specs[target])
                cleanup_ok = not needs_start or not self.driver.running(self.specs[target])
            except Exception:
                pass
            with self.lock:
                if cleanup_ok:
                    self.handoff = None
                else:
                    # Keep the admission block; the expiry reaper retries
                    # cleanup without touching either original resident.
                    self.handoff['expires'] = self.clock()
            if not cleanup_ok:
                raise AdmissionError(503, 'HANDOFF_ROLLBACK_PENDING')
            raise
        finally:
            with self.lock:
                if needs_start:
                    self.transitioning.discard(target)

    def handoff_finish(self, owner, *, completed=True, ticket=None):
        """Finish or cancel; a busy model is never stopped."""
        with self.lock:
            state = self.handoff
            if state is None or state['owner'] != owner:
                raise AdmissionError(404, 'HANDOFF_NOT_OWNED')
            if ticket is not None and not hmac.compare_digest(state['ticket'], ticket):
                raise AdmissionError(403, 'HANDOFF_TICKET_INVALID')
            if self.transitioning:
                raise AdmissionError(429, 'MODEL_TRANSITION_BUSY')
            victim = state['from'] if completed else (state['to'] if state.get('started_target') else None)
            if completed and victim not in self.running():
                # The source model can be nonresident in a general handoff.
                # Reclaim an idle unrelated resident only if a third exists.
                loaded = self.running()
                if len(loaded) > self.max_loaded:
                    candidates = [m for m in loaded if m != state['to']
                                  and self.active[m] == 0]
                    if not candidates:
                        raise AdmissionError(429, 'HANDOFF_MODEL_LEASED')
                    victim = min(candidates,
                                 key=lambda m:(self.specs[m]['warm'],
                                               self.used[m] or 0, m))
            if victim is None:
                self.handoff = None
                return
            if self.active[victim]:
                raise AdmissionError(429, 'HANDOFF_MODEL_LEASED')
            self.transitioning.add(victim)
        try:
            # Recovery is idempotent: a previous stop may have succeeded even
            # when its confirmation failed. Never stop an already-absent model.
            if self.driver.running(self.specs[victim]):
                self.driver.stop(self.specs[victim])
            if self.driver.running(self.specs[victim]):
                raise AdmissionError(503, 'HANDOFF_STOP_NOT_CONFIRMED')
            with self.lock:
                self.handoff = None
        finally:
            with self.lock:
                self.transitioning.discard(victim)

    def handoff_expire(self):
        with self.lock:
            state = self.handoff
            if state is None or state['expires'] is None or self.clock() < state['expires']:
                return False
            owner = state['owner']
        self.handoff_finish(owner, completed=False)
        return True

    def reap(self):
        """Reclaim any idle backend without automatically restoring a fixed warm pair.

        A transition is reserved under the lock, but slow Docker stop/start
        runs outside it. Acquiring the transitioning model fails closed; an
        already-ready model may still acquire a lease concurrently.
        """
        for _ in range(2 * len(self.specs)):
            with self.lock:
                if self.observe_only or self.transitioning or self.handoff:
                    return
                loaded = self.running()
                choice = None
                for model in loaded:
                    if self.active[model]:
                        continue
                    last = self.used[model]
                    if last is not None and self.clock() - last >= self.idle_seconds:
                        choice = ('stop', model)
                        break
                if choice is None:
                    return
                action, model = choice
                self.transitioning.add(model)
            try:
                spec = self.specs[model]
                if action == 'stop':
                    self.driver.stop(spec)
                    if self.driver.running(spec):
                        raise AdmissionError(503, 'IDLE_STOP_NOT_CONFIRMED')
                else:
                    self.driver.start(spec)
                    if not self.driver.running(spec):
                        raise AdmissionError(503, 'WARM_RESTORE_NOT_CONFIRMED')
            finally:
                with self.lock:
                    self.transitioning.discard(model)
        raise AdmissionError(503, 'REAPER_TRANSITION_LIMIT')


def make_handler(coordinator, model, api_key):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'AICoreModelGate/1'
        def log_message(self, fmt, *args):
            return
        def _auth(self):
            return hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+api_key)
        def _send(self, status, payload):
            raw = json.dumps(payload,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if self.path == '/health':
                self._send(200,{'status':'ok','gate_model':model})
                return
            if not self._auth():
                self._send(401,{'error':{'code':'UNAUTHORIZED'}})
                return
            if self.path == '/internal/handoff/status':
                with coordinator.lock:
                    handoff = coordinator.handoff
                    active = dict(coordinator.active)
                    summary = None if handoff is None else {
                        'from': handoff['from'], 'to': handoff['to'],
                        'expires': handoff['expires']}
                # Docker inspect can be slow. A monitoring GET must not
                # hold the global lease lock during that readback.
                try:
                    resident = sorted(coordinator.running())
                except AdmissionError as error:
                    self._send(error.status, {'error': {'code': error.code}})
                    return
                except (OSError, subprocess.TimeoutExpired):
                    self._send(503, {'error': {'code': 'STATUS_BACKEND_UNAVAILABLE'}})
                    return
                result = {'resident': resident,
                          'active': active, 'handoff': summary}
                self._send(200, result)
                return
            if self.path == '/v1/models':
                mid = coordinator.specs[model]['model_id']
                self._send(200,{'object':'list','data':[{'id':mid,'object':'model'}]})
                return
            self._send(404,{'error':{'code':'NOT_FOUND'}})
        def do_POST(self):
            if not self._auth():
                self._send(401,{'error':{'code':'UNAUTHORIZED'}})
                return
            if self.path in ('/internal/handoff/begin', '/internal/handoff/complete',
                             '/internal/handoff/abort'):
                if coordinator.observe_only:
                    self._send(409, {'error': {'code': 'OBSERVE_ONLY_NO_HANDOFF'}})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '-1'))
                    if not 1 <= length <= 4096:
                        raise AdmissionError(413, 'INVALID_REQUEST_SIZE')
                    data = json.loads(self.rfile.read(length).decode('utf-8'))
                    if not isinstance(data, dict):
                        raise AdmissionError(422, 'INVALID_HANDOFF_REQUEST')
                    if self.path == '/internal/handoff/begin':
                        result = coordinator.handoff_begin(
                            model, data.get('target'), data.get('owner'),
                            data.get('ttl', 120))
                        self._send(200, result)
                    else:
                        ticket = data.get('ticket')
                        if not isinstance(ticket, str) or not ticket:
                            raise AdmissionError(422, 'HANDOFF_TICKET_REQUIRED')
                        coordinator.handoff_finish(
                            data.get('owner'),
                            completed=self.path.endswith('/complete'),
                            ticket=ticket)
                        self._send(200, {'handoff': 'finished'})
                except AdmissionError as error:
                    self._send(error.status, {'error': {'code': error.code}})
                except (ValueError, UnicodeDecodeError, TypeError):
                    self._send(400, {'error': {'code': 'BAD_REQUEST'}})
                return
            if self.path != '/v1/chat/completions':
                self._send(404,{'error':{'code':'NOT_FOUND'}})
                return
            # A parallel legacy router holds no model-gate lease. During the
            # four-resident compatibility stage, probes may inspect /v1/models
            # but may not dispatch unleased inference to -np 1 backends.
            if coordinator.observe_only:
                self._send(409,{'error':{'code':'OBSERVE_ONLY_NO_INFERENCE'}})
                return
            try:
                length = int(self.headers.get('Content-Length','-1'))
                if length < 1 or length > MAX_REQUEST_BYTES:
                    raise AdmissionError(413,'INVALID_REQUEST_SIZE')
                obj = json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(obj,dict) or not isinstance(obj.get('stream',False),bool):
                    raise AdmissionError(422,'INVALID_STREAM_FLAG')
                target_id = coordinator.specs[model]['model_id']
                if obj.get('model') not in (target_id,model+'/'+target_id):
                    raise AdmissionError(422,'WRONG_MODEL_FOR_ENDPOINT')
                obj['model'] = target_id
                body = json.dumps(obj,ensure_ascii=False).encode('utf-8')
                stream_headers_sent = False
                with coordinator.lease(model) as spec:
                    conn = http.client.HTTPConnection('127.0.0.1',spec['backend_port'],timeout=600)
                    try:
                        conn.request('POST','/v1/chat/completions',body=body,
                                     headers={'Content-Type':'application/json'})
                        response = conn.getresponse()
                        if obj.get('stream',False):
                            if response.status != 200 or not response.getheader('Content-Type','').lower().startswith('text/event-stream'):
                                raise AdmissionError(502,'UPSTREAM_STREAM_CONTRACT_INVALID')
                            # Keep the shared model lease until the upstream SSE
                            # stream terminates (including client disconnect).
                            self.send_response(200)
                            self.send_header('Content-Type','text/event-stream')
                            self.send_header('Cache-Control','no-cache')
                            self.send_header('Connection','close')
                            self.end_headers()
                            stream_headers_sent = True
                            self.close_connection = True
                            total = 0
                            done = False
                            tail = b''
                            stream_error = None
                            while True:
                                chunk = response.read1(8192)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > MAX_RESPONSE_BYTES:
                                    stream_error = 'UPSTREAM_STREAM_TOO_LARGE'
                                    break
                                # The final SSE marker may span read boundaries.
                                combined = tail + chunk
                                if b'data: [DONE]' in combined:
                                    done = True
                                tail = combined[-32:]
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            if stream_error or not done:
                                code = stream_error or 'UPSTREAM_STREAM_INCOMPLETE'
                                self.wfile.write(('\\n\\nevent: error\\ndata: ' +
                                    json.dumps({'error': {'code': code}}) +
                                    '\\n\\n').encode('utf-8'))
                                self.wfile.flush()
                        else:
                            if int(response.getheader('Content-Length','0')) > MAX_RESPONSE_BYTES:
                                raise AdmissionError(502,'UPSTREAM_RESPONSE_TOO_LARGE')
                            upstream = response.read(MAX_RESPONSE_BYTES+1)
                            if len(upstream) > MAX_RESPONSE_BYTES:
                                raise AdmissionError(502,'UPSTREAM_RESPONSE_TOO_LARGE')
                            self.send_response(response.status)
                            self.send_header('Content-Type','application/json')
                            self.send_header('Content-Length',str(len(upstream)))
                            self.end_headers()
                            self.wfile.write(upstream)
                    finally:
                        conn.close()
            except AdmissionError as error:
                if not locals().get('stream_headers_sent', False):
                    self._send(error.status,{'error':{'code':error.code}})
                else:
                    self.close_connection = True
            except (ValueError,UnicodeDecodeError):
                if not locals().get('stream_headers_sent', False):
                    self._send(400,{'error':{'code':'BAD_REQUEST'}})
                else:
                    self.close_connection = True
            except (OSError,TimeoutError,subprocess.TimeoutExpired):
                # HTTP headers cannot be sent twice after SSE has started.
                if not locals().get('stream_headers_sent', False):
                    self._send(503,{'error':{'code':'UPSTREAM_UNAVAILABLE'}})
                else:
                    self.close_connection = True
    return Handler


def run_servers(coordinator, token, state_dir):
    if not token:
        raise ValueError('EXISTING_API_KEY_REQUIRED')
    folder = Path(state_dir)
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    lockfile = folder/'per-model-gate.lock'
    with lockfile.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AdmissionError(409,'GATE_ALREADY_RUNNING') from error
        coordinator.preflight()
        servers = []
        try:
            for model,spec in coordinator.specs.items():
                server = ThreadingHTTPServer(('127.0.0.1',spec['gate_port']),
                                             make_handler(coordinator,model,token))
                server.daemon_threads = True
                servers.append(server)
            for server in servers:
                threading.Thread(target=server.serve_forever,daemon=True).start()
            stop = threading.Event()
            def on_signal(*_):
                stop.set()
            signal.signal(signal.SIGINT,on_signal)
            signal.signal(signal.SIGTERM,on_signal)
            while not stop.wait(2):
                try:
                    coordinator.handoff_expire()
                except AdmissionError:
                    # A Docker stop failure or leased model must not terminate
                    # the sole gate process and strand a third resident.
                    # The reservation stays set, and next tick retries.
                    pass
                try:
                    coordinator.reap()
                except (AdmissionError, OSError, subprocess.TimeoutExpired):
                    # A transient Docker failure must not kill the only
                    # shared gate. Transition markers are cleared by reap().
                    # The next maintenance cycle retries eligible models.
                    pass
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--compose',default=str(Path(__file__).resolve().parents[1]/'compose.runtime.yml'))
    parser.add_argument('--policy',default=str(DEFAULT_POLICY))
    parser.add_argument('--preflight',action='store_true')
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--exclusive-traffic',action='store_true')
    parser.add_argument('--observe-only',action='store_true',
                        help='never start/stop models; allow legacy 4-resident Host')
    parser.add_argument('--state-dir',default='/home/admin1/projects/ai-core/.ai-core')
    args = parser.parse_args(argv)
    if args.preflight == args.serve:
        parser.error('choose exactly one of --preflight or --serve')
    if args.observe_only and (not args.serve or args.exclusive_traffic):
        parser.error('--observe-only is a non-mutating --serve mode only')
    policy = load_policy(args.policy)
    specs = model_specs_from_compose(args.compose,policy)
    if args.preflight:
        print('MODEL_GATE_PREFLIGHT=PASS MODEL_COUNT='+str(len(specs))+
              ' PORTS='+','.join(str(x['gate_port']) for x in specs.values()))
        return 0
    if not (args.exclusive_traffic or args.observe_only):
        parser.error('--serve requires --exclusive-traffic or --observe-only')
    key = os.environ.get('LLAMA_SWAP_API_KEY','')
    if len(key) < 16:
        parser.error('existing LLAMA_SWAP_API_KEY environment must be configured')
    budget = verify_parent_budget(specs,policy)
    print('MODEL_GATE_PARENT_BUDGET=PASS CPU_CORES='+str(budget['parent_cpu_cores'])+
          ' MAX_BYTES='+str(budget['parent_memory_max']))
    driver = ExistingContainerDriver(args.compose)
    coordinator = Coordinator(specs,driver,max_loaded=policy['max_resident'],
                              max_active=policy['max_active'],
                              idle_seconds=policy['idle_seconds'],
                              observe_only=args.observe_only)
    print('MODEL_GATE_MODE='+('OBSERVE_ONLY_NO_LIFECYCLE' if args.observe_only
                              else 'EXCLUSIVE_RESIDENCY'))
    run_servers(coordinator,key,args.state_dir)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AdmissionError,ValueError) as error:
        code = error.code if isinstance(error,AdmissionError) else str(error)
        print('MODEL_GATE=FAIL|'+code,file=sys.stderr)
        sys.exit(1)
