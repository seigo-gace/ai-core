import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runtime.model_gate import (AdmissionError, Coordinator, ExistingContainerDriver, load_policy,
    make_handler, model_specs_from_compose, parse_bytes, verify_parent_budget)


class FakeDriver:
    def __init__(self, running=()):
        self.loaded=set(running)
        self.ops=[]
        self.ready=True
    def running(self, spec):
        return spec['service'] in self.loaded
    def healthy(self, spec):
        return self.ready
    def start(self, spec):
        self.ops.append(('start',spec['service']))
        self.loaded.add(spec['service'])
    def stop(self, spec):
        self.ops.append(('stop',spec['service']))
        self.loaded.remove(spec['service'])


def specs():
    return {name:{'service':name,'model_id':'/models/'+name+'.gguf',
      'backend_port':20000+i,'gate_port':21000+i,
      'warm':False,'memory_limit_bytes':9216000000,
      'cpu_limit':3.0}
      for i,name in enumerate(('granite','qwen3','ministral','coder'))}


class SourceTests(unittest.TestCase):
    def test_policy_and_four_model_contract(self):
        policy=load_policy(ROOT/'runtime/model-policy.yaml')
        self.assertEqual((policy['max_resident'],policy['max_active']),(2,2))
        self.assertEqual({m for m,x in policy['models'].items() if x['warm']},
                         set())
        items=model_specs_from_compose(ROOT/'compose.runtime.yml',policy)
        self.assertEqual(set(items),set(specs()))
        self.assertEqual(items['ministral']['backend_port'],18083)
        self.assertEqual(items['coder']['model_id'],
                         '/models/qwen2.5-coder-7b-instruct-q4_k_m.gguf')
        self.assertEqual(items['qwen3']['memory_limit_bytes'],9216*(1024**2))
    def test_policy_rejects_boolean_numeric_fields(self):
        import copy
        import yaml
        policy=load_policy(ROOT/'runtime/model-policy.yaml')
        variants=(
            ('gate_port', lambda p: p['models']['coder'].__setitem__('gate_port', True),
             'INVALID_GATE_PORT'),
            ('max_resident', lambda p: p.__setitem__('max_resident', True),
             'INVALID_ADMISSION_LIMITS'),
            ('max_active', lambda p: p.__setitem__('max_active', True),
             'INVALID_ADMISSION_LIMITS'),
            ('idle_seconds', lambda p: p.__setitem__('idle_seconds', True),
             'INVALID_IDLE_SECONDS'),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'policy.yaml'
            for label,mutate,error_code in variants:
                with self.subTest(field=label):
                    candidate=copy.deepcopy(policy)
                    mutate(candidate)
                    path.write_text(yaml.safe_dump(candidate),encoding='utf-8')
                    with self.assertRaisesRegex(ValueError,error_code):
                        load_policy(path)

    def test_policy_rejects_non_string_model_identifiers(self):
        import copy
        import yaml
        policy=load_policy(ROOT/'runtime/model-policy.yaml')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'policy.yaml'
            for bad_key in (True, 42, None):
                with self.subTest(model_key=repr(bad_key)):
                    candidate=copy.deepcopy(policy)
                    candidate['models'][bad_key]=candidate['models'].pop('coder')
                    path.write_text(yaml.safe_dump(candidate),encoding='utf-8')
                    with self.assertRaisesRegex(ValueError,'INVALID_SERVICE_NAME'):
                        load_policy(path)

    def test_duplicate_gate_port_rejected(self):
        text=(ROOT/'runtime/model-policy.yaml').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'policy.yaml'
            path.write_text(text.replace('gate_port: 18182','gate_port: 18181'))
            with self.assertRaisesRegex(ValueError,'DUPLICATE_GATE_PORT'):
                load_policy(path)
    def test_memory_input_rejects_invalid_values(self):
        self.assertEqual(parse_bytes('9216m'),9216*(1024**2))
        for value in ('0m','max','5g; rm','-1g'):
            with self.assertRaises(ValueError):
                parse_bytes(value)
    def test_existing_parent_budget_preserved(self):
        policy=load_policy(ROOT/'runtime/model-policy.yaml')
        items=model_specs_from_compose(ROOT/'compose.runtime.yml',policy)
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'ai-core.slice'
            folder.mkdir()
            (folder/'memory.max').write_text('36000000000')
            (folder/'memory.high').write_text('34000000000')
            (folder/'cpu.max').write_text('600000 100000')
            budget=verify_parent_budget(items,policy,cgroup_root=Path(tmp),
                                        control_group='/ai-core.slice')
            self.assertEqual(budget['parent_cpu_cores'],6)
            (folder/'cpu.max').write_text('200000 100000')
            with self.assertRaisesRegex(AdmissionError,'EXCEED_PARENT_BUDGET'):
                verify_parent_budget(items,policy,cgroup_root=Path(tmp),
                                     control_group='/ai-core.slice')
            (folder/'cpu.max').write_text('max 100000')
            with self.assertRaisesRegex(AdmissionError,'PARENT_BUDGET_UNAVAILABLE'):
                verify_parent_budget(items,policy,cgroup_root=Path(tmp),
                                     control_group='/ai-core.slice')


class CoordinatorTests(unittest.TestCase):
    def test_old_four_resident_not_stopped_automatically(self):
        d=FakeDriver(specs())
        c=Coordinator(specs(),d)
        with self.assertRaisesRegex(AdmissionError,'EXISTING_RESIDENCY_EXCEEDS_POLICY'):
            c.preflight()
        self.assertEqual(d.ops,[])
    def test_observe_only_accepts_legacy_four_without_stopping(self):
        driver=FakeDriver(specs())
        coordinator=Coordinator(specs(),driver,observe_only=True)
        self.assertEqual(coordinator.preflight(),set(specs()))
        with coordinator.lease('qwen3'):
            self.assertEqual(coordinator.active['qwen3'],1)
            coordinator.reap()
        self.assertEqual(driver.ops,[])
        self.assertEqual(driver.loaded,set(specs()))

    def test_observe_only_denies_cold_load_without_any_mutation(self):
        driver=FakeDriver(('coder','qwen3'))
        coordinator=Coordinator(specs(),driver,observe_only=True)
        self.assertEqual(len(coordinator.preflight()),2)
        with self.assertRaisesRegex(AdmissionError,'OBSERVE_ONLY_MODEL_NOT_RUNNING'):
            coordinator.acquire('granite')
        self.assertEqual(driver.ops,[])
        self.assertEqual(driver.loaded,{'coder','qwen3'})
        with coordinator.lease('coder'):
            self.assertEqual(coordinator.active['coder'],1)
        self.assertEqual(coordinator.active['coder'],0)

    def test_observe_only_reaper_does_not_warm_stopped_services(self):
        driver=FakeDriver(('ministral',))
        now=[100.0]
        coordinator=Coordinator(specs(),driver,observe_only=True,
                                idle_seconds=10,clock=lambda:now[0])
        with coordinator.lease('ministral'):
            now[0]=500.0
        coordinator.reap()
        self.assertEqual(driver.loaded,{'ministral'})
        self.assertEqual(driver.ops,[])

    def test_cold_start_reuses_warm_slot(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d)
        with c.lease('granite'):
            self.assertEqual(len(d.loaded),2)
            self.assertIn('granite',d.loaded)
        self.assertEqual(d.ops[:2],[('stop','coder'),('start','granite')])
    def test_two_leased_models_do_not_allow_third_request(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d)
        with c.lease('coder'):
            with c.lease('qwen3'):
                with self.assertRaisesRegex(AdmissionError,'GLOBAL_CONCURRENCY_LIMIT'):
                    c.acquire('ministral')
        self.assertEqual(d.ops,[])
    def test_one_resident_lease_never_evicted(self):
        d=FakeDriver(('coder',))
        c=Coordinator(specs(),d,max_loaded=1,max_active=1)
        with c.lease('coder'):
            with self.assertRaises(AdmissionError):
                c.acquire('granite')
        self.assertEqual(d.ops,[])
    def test_busy_single_model_denied_without_state_loss(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d)
        with c.lease('coder'):
            with self.assertRaisesRegex(AdmissionError,'MODEL_CONCURRENCY_LIMIT'):
                c.acquire('coder')
            self.assertEqual(c.active['coder'],1)
        self.assertEqual(c.active['coder'],0)

    def test_slow_cold_eviction_does_not_block_other_ready_model(self):
        stopped=threading.Event()
        allow_stop=threading.Event()
        errors=[]
        class SlowEvictionDriver(FakeDriver):
            def stop(self, spec):
                stopped.set()
                if not allow_stop.wait(3):
                    raise TimeoutError('cold eviction stalled')
                super().stop(spec)
        driver=SlowEvictionDriver(('coder','qwen3'))
        controller=Coordinator(specs(),driver)
        def cold():
            try:
                with controller.lease('granite'):
                    pass
            except Exception as exc:
                errors.append(exc)
        worker=threading.Thread(target=cold,daemon=True)
        worker.start()
        try:
            self.assertTrue(stopped.wait(2),'eviction did not begin')
            # Coder is chosen as victim by the existing LRU tie-breaker.
            # The other resident must remain immediately leasable.
            with controller.lease('qwen3'):
                self.assertEqual(controller.active['qwen3'],1)
            with self.assertRaisesRegex(AdmissionError,'MODEL_TRANSITION_BUSY'):
                controller.acquire('coder')
        finally:
            allow_stop.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(controller.transitioning,set())
        self.assertEqual(len(driver.loaded),2)

    def test_idle_unload_does_not_hold_lock_for_ready_warm_request(self):
        stopped=threading.Event()
        allow_stop=threading.Event()
        errors=[]
        class SlowStopDriver(FakeDriver):
            def stop(self, spec):
                stopped.set()
                if not allow_stop.wait(3):
                    raise TimeoutError('test stop stalled')
                super().stop(spec)
        driver=SlowStopDriver(('coder','ministral'))
        now=[10.0]
        coordinator=Coordinator(specs(),driver,idle_seconds=10,
                                clock=lambda:now[0])
        with coordinator.lease('ministral'):
            pass
        now[0]=50.0
        def maintenance():
            try:
                coordinator.reap()
            except Exception as exc:
                errors.append(exc)
        worker=threading.Thread(target=maintenance,daemon=True)
        worker.start()
        try:
            self.assertTrue(stopped.wait(2),'idle unload did not begin')
            with coordinator.lease('coder'):
                self.assertEqual(coordinator.active['coder'],1)
            with self.assertRaisesRegex(AdmissionError,'MODEL_TRANSITION_BUSY'):
                coordinator.acquire('ministral')
        finally:
            allow_stop.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(driver.loaded,{'coder'})
        self.assertEqual(coordinator.transitioning,set())

    def test_failed_idle_stop_does_not_poison_transition_and_can_retry(self):
        class TransientStop(FakeDriver):
            fail=True
            def stop(self, spec):
                if self.fail:
                    raise AdmissionError(503, 'EXISTING_CONTAINER_COMMAND_FAILED')
                super().stop(spec)
        d=TransientStop(('coder',))
        clock=[0.0]
        c=Coordinator(specs(),d,idle_seconds=10,clock=lambda:clock[0])
        with c.lease('coder'):
            pass
        clock[0]=11.0
        with self.assertRaisesRegex(AdmissionError,'EXISTING_CONTAINER_COMMAND_FAILED'):
            c.reap()
        self.assertEqual(c.transitioning,set())
        self.assertEqual(d.loaded,{'coder'})
        d.fail=False
        c.reap()
        self.assertEqual(d.loaded,set())

    def test_four_models_rotate_through_two_slots_on_demand(self):
        driver=FakeDriver(('coder','qwen3'))
        controller=Coordinator(specs(),driver,idle_seconds=10)
        self.assertEqual(len(driver.loaded),2)
        for requested in ('granite','ministral','coder','qwen3'):
            with controller.lease(requested):
                self.assertIn(requested,driver.loaded)
                self.assertLessEqual(len(driver.loaded),2)
            self.assertLessEqual(len(driver.loaded),2)
        self.assertEqual(driver.loaded,{'coder','qwen3'})
        self.assertEqual([op for op,_ in driver.ops if op=='start'],
                         ['start']*4)

    def test_handoff_ticket_prevents_cross_project_completion(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d,handoff_budget_check=lambda: {
            'selected_memory_ceiling':27, 'selected_cpu_ceiling':6})
        state=c.handoff_begin('coder','granite','project-a')
        self.assertGreaterEqual(len(state['ticket']),32)
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_TICKET_INVALID'):
            c.handoff_finish('project-a',ticket='invalid')
        self.assertEqual(d.loaded,{'coder','qwen3','granite'})
        c.handoff_finish('project-a',ticket=state['ticket'])
        self.assertEqual(d.loaded,{'granite','qwen3'})

    def test_live_parent_budget_denies_third_at_memory_high(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d)
        budget={'parent_memory_max':36*1024**3,
                'parent_memory_high':34*1024**3,
                'parent_cpu_cores':6,
                'selected_memory_ceiling':18*1024**3,
                'selected_cpu_ceiling':6}
        process=SimpleNamespace(returncode=0,stdout='/ai.slice/ai-core.slice')
        real_read=Path.read_text
        def memory_file(path,*args,**kwargs):
            if str(path).endswith('/memory.current'):
                return str(27*1024**3)
            return real_read(path,*args,**kwargs)
        with patch('runtime.model_gate.verify_parent_budget',return_value=budget):
            with patch('runtime.model_gate.subprocess.run',return_value=process):
                with patch('pathlib.Path.read_text',new=memory_file):
                    with self.assertRaisesRegex(AdmissionError,'HANDOFF_INSUFFICIENT_LIVE_HEADROOM'):
                        c.handoff_begin('coder','granite','p')
        self.assertEqual(d.ops,[])
        self.assertEqual(d.loaded,{'coder','qwen3'})
        self.assertIsNone(c.handoff)

    def test_live_parent_budget_allows_third_with_headroom(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d)
        budget={'parent_memory_max':36*1024**3,
                'parent_memory_high':34*1024**3,
                'parent_cpu_cores':6,
                'selected_memory_ceiling':18*1024**3,
                'selected_cpu_ceiling':6}
        process=SimpleNamespace(returncode=0,stdout='/ai.slice/ai-core.slice')
        original_read=Path.read_text
        def current_file(path,*args,**kwargs):
            if str(path).endswith('/memory.current'):
                return str(18*1024**3)
            return original_read(path,*args,**kwargs)
        with patch('runtime.model_gate.verify_parent_budget',return_value=budget):
            with patch('runtime.model_gate.subprocess.run',return_value=process):
                with patch('pathlib.Path.read_text',new=current_file):
                    state=c.handoff_begin('coder','granite','p')
        self.assertEqual(len(d.loaded),3)
        c.handoff_finish('p',ticket=state['ticket'])
        self.assertEqual(d.loaded,{'granite','qwen3'})

    def test_slow_handoff_budget_does_not_block_ready_model(self):
        entered=threading.Event()
        resume=threading.Event()
        issues=[]
        driver=FakeDriver(('coder','qwen3'))
        def slow_budget():
            entered.set()
            if not resume.wait(3):
                raise TimeoutError('budget check blocked')
            return {'selected_memory_ceiling':27,'selected_cpu_ceiling':6}
        c=Coordinator(specs(),driver,handoff_budget_check=slow_budget)
        def begin():
            try:
                c.handoff_begin('coder','granite','project')
            except Exception as error:
                issues.append(error)
        t=threading.Thread(target=begin,daemon=True)
        t.start()
        try:
            self.assertTrue(entered.wait(2))
            with c.lease('qwen3'):
                self.assertEqual(c.active['qwen3'],1)
            self.assertEqual(driver.loaded,{'coder','qwen3'})
        finally:
            resume.set()
            t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertEqual(issues,[])
        self.assertEqual(len(driver.loaded),3)
        c.handoff_finish('project')

    def test_handoff_budget_failure_never_starts_third(self):
        d=FakeDriver(('coder','qwen3'))
        def deny():
            raise AdmissionError(409, 'HANDOFF_MEMORY_CEILING_EXCEEDED')
        c=Coordinator(specs(),d,handoff_budget_check=deny)
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_MEMORY_CEILING_EXCEEDED'):
            c.handoff_begin('coder','granite','project-a')
        self.assertEqual(d.loaded,{'coder','qwen3'})
        self.assertEqual(d.ops,[])
        self.assertIsNone(c.handoff)

    def test_handoff_uses_temporary_third_and_reclaims_old(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d,handoff_budget_check=lambda: {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        state=c.handoff_begin('coder','granite','project-a',ttl=60)
        self.assertEqual(state['owner'],'project-a')
        self.assertEqual(d.loaded,{'coder','qwen3','granite'})
        with c.lease('qwen3'):
            with c.lease('granite'):
                self.assertEqual(len(d.loaded),3)
        c.handoff_finish('project-a')
        self.assertEqual(d.loaded,{'qwen3','granite'})
        self.assertIsNone(c.handoff)

    def test_handoff_abort_and_expiry_reclaim_new_model(self):
        d=FakeDriver(('coder','qwen3'))
        now=[100.0]
        c=Coordinator(specs(),d,clock=lambda:now[0],handoff_budget_check=lambda: {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        c.handoff_begin('coder','ministral','project-a',ttl=10)
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_IN_PROGRESS'):
            c.handoff_begin('qwen3','granite','project-b')
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_NOT_OWNED'):
            c.handoff_finish('project-b')
        now[0]=111.0
        self.assertTrue(c.handoff_expire())
        self.assertEqual(d.loaded,{'coder','qwen3'})
        self.assertFalse(c.handoff_expire())

    def test_handoff_lease_protects_busy_old_model(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d,handoff_budget_check=lambda: {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        c.handoff_begin('coder','granite','p')
        with c.lease('coder'):
            with self.assertRaisesRegex(AdmissionError,'HANDOFF_MODEL_LEASED'):
                c.handoff_finish('p')
            self.assertEqual(len(d.loaded),3)
        c.handoff_finish('p')
        self.assertEqual(d.loaded,{'qwen3','granite'})

    def test_failed_handoff_stop_preserves_reservation_and_retries(self):
        class StickyStop(FakeDriver):
            fail_stop = True
            def healthy(self, spec):
                return False if spec['service'] == 'granite' else True
            def stop(self, spec):
                if spec['service'] == 'granite' and self.fail_stop:
                    raise RuntimeError('docker stop unavailable')
                super().stop(spec)
        driver=StickyStop(('coder','qwen3'))
        ticks=[0]
        def clock():
            ticks[0]+=1
            return float(ticks[0])
        controller=Coordinator(specs(),driver,clock=clock,start_timeout=1,handoff_budget_check=lambda: {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_ROLLBACK_PENDING'):
            controller.handoff_begin('coder','granite','project-a')
        self.assertEqual(driver.loaded,{'coder','qwen3','granite'})
        self.assertIsNotNone(controller.handoff)
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_IN_PROGRESS'):
            controller.handoff_begin('qwen3','ministral','project-b')
        driver.fail_stop=False
        self.assertTrue(controller.handoff_expire())
        self.assertEqual(driver.loaded,{'coder','qwen3'})
        self.assertIsNone(controller.handoff)

    def test_handoff_expired_busy_lease_keeps_reservation(self):
        d=FakeDriver(('coder','qwen3'))
        now=[100.0]
        c=Coordinator(specs(),d,clock=lambda:now[0],handoff_budget_check=lambda: {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        c.handoff_begin('coder','granite','project-a',ttl=2)
        now[0]=103.0
        with c.lease('granite'):
            with self.assertRaisesRegex(AdmissionError,'HANDOFF_MODEL_LEASED'):
                c.handoff_expire()
            self.assertIsNotNone(c.handoff)
        self.assertTrue(c.handoff_expire())
        self.assertEqual(d.loaded,{'coder','qwen3'})

    def test_handoff_denies_legacy_four_and_observe_only(self):
        d=FakeDriver(specs())
        c=Coordinator(specs(),d,handoff_budget_check=lambda: {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_RESIDENCY_NOT_READY'):
            c.handoff_begin('coder','granite','p')
        self.assertEqual(d.ops,[])
        c=Coordinator(specs(),FakeDriver(('coder','qwen3')),observe_only=True)
        with self.assertRaisesRegex(AdmissionError,'OBSERVE_ONLY_NO_HANDOFF'):
            c.handoff_begin('coder','granite','p')

    def test_reaper_does_not_relaunch_fixed_models(self):
        driver=FakeDriver(('coder',))
        controller=Coordinator(specs(),driver)
        controller.reap()
        self.assertEqual(driver.loaded,{'coder'})
        self.assertEqual(driver.ops,[])
        with controller.lease('qwen3'):
            self.assertEqual(driver.loaded,{'coder','qwen3'})
        self.assertEqual(driver.ops,[('start','qwen3')])

    def test_idle_release_does_not_autorestore_warm_pair(self):
        d=FakeDriver(('coder','ministral'))
        now=[100.0]
        c=Coordinator(specs(),d,idle_seconds=10,clock=lambda:now[0])
        with c.lease('ministral'):
            now[0]=140.0
            c.reap()
            self.assertIn('ministral',d.loaded)
        now[0]=160.0
        c.reap()
        self.assertEqual(d.loaded,{'coder'})
    def test_failed_cold_start_restores_previous_resident(self):
        d=FakeDriver(('coder','qwen3'))
        d.ready=False
        ticks=[0]
        def clock():
            ticks[0]+=1
            return ticks[0]
        c=Coordinator(specs(),d,start_timeout=1,clock=clock)
        with self.assertRaisesRegex(AdmissionError,'MODEL_START_TIMEOUT'):
            c.acquire('granite')
        self.assertEqual(d.loaded,{'coder','qwen3'})
        self.assertEqual(c.active['granite'],0)
    def test_partial_eviction_failure_restores_previous_residents(self):
        class StopAckFailure(FakeDriver):
            fail_once = True
            def stop(self, spec):
                super().stop(spec)
                if self.fail_once:
                    self.fail_once = False
                    raise OSError('stop acknowledgment lost after process stopped')
        driver=StopAckFailure(('coder','qwen3'))
        controller=Coordinator(specs(),driver)
        with self.assertRaisesRegex(OSError,'acknowledgment lost'):
            controller.acquire('granite')
        self.assertEqual(driver.loaded,{'coder','qwen3'})
        self.assertEqual(controller.active['granite'],0)
        self.assertEqual([op for op in driver.ops if op[0]=='start'],
                         [('start','coder')])
        # Semaphore is released even when the first transition failed.
        with controller.lease('qwen3'):
            self.assertEqual(controller.active['qwen3'],1)

    def test_ready_warm_model_can_serve_during_cold_load(self):
        started=threading.Event()
        ready=threading.Event()
        error=[]
        class SlowColdDriver(FakeDriver):
            def healthy(self, spec):
                started.set()
                return ready.is_set()
        driver=SlowColdDriver(('coder','qwen3'))
        controller=Coordinator(specs(),driver,start_timeout=15)
        def cold():
            try:
                with controller.lease('granite'):
                    pass
            except Exception as exc:
                error.append(exc)
        worker=threading.Thread(target=cold,daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(2),'cold startup did not begin')
            before=__import__('time').monotonic()
            with controller.lease('qwen3'):
                self.assertEqual(controller.active['qwen3'],1)
            elapsed=__import__('time').monotonic()-before
            self.assertLess(elapsed,2.0,'warm request blocked on cold readiness')
            snapshot=list(driver.ops)
            controller.reap()
            self.assertEqual(driver.ops,snapshot,'reaper mutated during transition')
        finally:
            ready.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive(),'cold transition did not finish')
        self.assertEqual(error,[])
        self.assertEqual(controller.transitioning,set())

    def test_second_cold_load_rejected_while_first_is_pending(self):
        started=threading.Event()
        ready=threading.Event()
        class SlowColdDriver(FakeDriver):
            def healthy(self, spec):
                started.set()
                return ready.is_set()
        driver=SlowColdDriver(('coder','qwen3'))
        controller=Coordinator(specs(),driver,start_timeout=15)
        worker=threading.Thread(target=lambda: controller.acquire('granite'),
                                daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            with self.assertRaisesRegex(AdmissionError,'MODEL_TRANSITION_BUSY'):
                controller.acquire('ministral')
            self.assertEqual(len(driver.loaded),2)
        finally:
            ready.set()
            worker.join(timeout=3)
            if controller.active['granite']:
                controller.release('granite')
        self.assertEqual(controller.transitioning,set())

    def test_multiple_model_switches_respect_two_resident_limit(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d)
        for model in ('ministral','granite','qwen3','coder'):
            with c.lease(model):
                self.assertLessEqual(len(d.loaded),2)


    def test_same_model_rejects_parallel_inference(self):
        d=FakeDriver(('coder','qwen3'))
        c=Coordinator(specs(),d)
        with c.lease('coder'):
            with self.assertRaisesRegex(AdmissionError,'MODEL_CONCURRENCY_LIMIT'):
                c.acquire('coder')
            with c.lease('qwen3'):
                self.assertEqual(c.active['qwen3'],1)
        self.assertEqual(c.active['coder'],0)


class ExistingContainerTests(unittest.TestCase):
    def setUp(self):
        self.spec=specs()['coder']
        self.record={
            'Name':'/ai-coder','State':{'Running':False},
            'Config':{'Cmd':['-m','/models/coder.gguf','--cache-ram','4096'],
                      'Labels':{'com.docker.compose.project':'ai-core',
                                'com.docker.compose.service':'coder'}},
            'HostConfig':{'CgroupParent':'ai-core.slice',
                          'Memory':9216000000,'NanoCpus':3000000000,
                          'PortBindings':{'8080/tcp':[{'HostIp':'127.0.0.1',
                                                      'HostPort':'20003'}]}}
        }
        self.driver=ExistingContainerDriver(ROOT/'compose.runtime.yml')
    def test_existing_container_start_never_recreates(self):
        calls=[]
        def run(args,**kwargs):
            calls.append(args)
            if args[1]=='inspect':
                return SimpleNamespace(returncode=0,stdout=json.dumps([self.record]))
            if args[1]=='start':
                self.record['State']['Running']=True
            if args[1]=='stop':
                self.record['State']['Running']=False
            return SimpleNamespace(returncode=0,stdout='')
        with patch('runtime.model_gate.subprocess.run',side_effect=run):
            self.driver.start(self.spec)
            self.assertTrue(self.driver.running(self.spec))
            self.driver.stop(self.spec)
        self.assertEqual([args[1] for args in calls if args[1] in ('start','stop')],
                         ['start','stop'])
        self.assertTrue(all('compose' not in args for args in calls))
    def test_wrong_parent_or_limits_fail_closed(self):
        def run(args,**kwargs):
            return SimpleNamespace(returncode=0,stdout=json.dumps([self.record]))
        with patch('runtime.model_gate.subprocess.run',side_effect=run):
            for field,broken in [('CgroupParent',''),('Memory',0),
                                 ('NanoCpus',0)]:
                original=self.record['HostConfig'][field]
                self.record['HostConfig'][field]=broken
                try:
                    with self.assertRaises(AdmissionError):
                        self.driver.start(self.spec)
                finally:
                    self.record['HostConfig'][field]=original
    def test_container_identity_and_loopback_protected(self):
        def run(args,**kwargs):
            return SimpleNamespace(returncode=0,stdout=json.dumps([self.record]))
        with patch('runtime.model_gate.subprocess.run',side_effect=run):
            self.record['HostConfig']['PortBindings']['8080/tcp'][0]['HostIp']='0.0.0.0'
            with self.assertRaisesRegex(AdmissionError,'LOOPBACK_PORT'):
                self.driver.running(self.spec)
            self.record['HostConfig']['PortBindings']['8080/tcp'][0]['HostIp']='127.0.0.1'
            self.record['Config']['Labels']['com.docker.compose.service']='other'
            with self.assertRaisesRegex(AdmissionError,'COMPOSE_OWNER'):
                self.driver.running(self.spec)


class GateHttpTests(unittest.TestCase):

    def setUp(self):
        self.driver=FakeDriver(('coder',))
        self.coord=Coordinator(specs(),self.driver)
        class Backend(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if data.get('stream') is True:
                    output=(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                            b'data: [DONE]\n\n')
                    self.send_response(200)
                    self.send_header('Content-Type','text/event-stream')
                    self.send_header('Content-Length',str(len(output)))
                    self.end_headers()
                    self.wfile.write(output)
                    return
                output=json.dumps({'choices':[{'message':{'content':data['model']}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(output)))
                self.end_headers()
                self.wfile.write(output)
        self.backend=ThreadingHTTPServer(('127.0.0.1',0),Backend)
        self.coord.specs['coder']['backend_port']=self.backend.server_address[1]
        self.gate=ThreadingHTTPServer(('127.0.0.1',0),
                          make_handler(self.coord,'coder','EXISTING_TEST_KEY_12345'))
        self.threads=[threading.Thread(target=x.serve_forever,daemon=True)
                      for x in (self.backend,self.gate)]
        for thread in self.threads:
            thread.start()
        self.base='http://127.0.0.1:'+str(self.gate.server_address[1])
    def tearDown(self):
        for server in (self.gate,self.backend):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=1)
    def req(self,path,body=None,auth=True):
        headers={'Content-Type':'application/json'}
        if auth: headers['Authorization']='Bearer EXISTING_TEST_KEY_12345'
        r=request.Request(self.base+path,headers=headers,
                          method='POST' if body is not None else 'GET',
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            with request.urlopen(r,timeout=3) as resp:
                return resp.status,json.load(resp)
        except error.HTTPError as exc:
            return exc.code,json.loads(exc.read())
    def test_unauthorized(self):
        status,_=self.req('/v1/models',auth=False)
        self.assertEqual(status,401)
    def test_model_id_available_before_loading(self):
        status,data=self.req('/v1/models')
        self.assertEqual(status,200)
        self.assertEqual(data['data'][0]['id'],'/models/coder.gguf')
    def test_alias_forwarded_as_native_model(self):
        status,data=self.req('/v1/chat/completions',
                             {'model':'coder//models/coder.gguf',
                              'messages':[{'role':'user','content':'ping'}],
                              'stream':False})
        self.assertEqual(status,200)
        self.assertEqual(data['choices'][0]['message']['content'],'/models/coder.gguf')
        self.assertEqual(self.coord.active['coder'],0)
    def test_four_existing_model_apis_share_one_coordinator(self):
        # Four model-local gates, not four independent resource managers.
        # Reuse the same mocked backend while verifying model IDs,
        # auth, capacity, and per-model OpenAI-compatible requests.
        ports = []
        for name in ('granite', 'qwen3', 'ministral'):
            self.coord.specs[name]['backend_port'] = self.backend.server_address[1]
            gate = ThreadingHTTPServer(
                ('127.0.0.1', 0),
                make_handler(self.coord, name, 'EXISTING_TEST_KEY_12345'))
            worker = threading.Thread(target=gate.serve_forever, daemon=True)
            worker.start()
            ports.append((name, gate, worker))
        try:
            all_gates = [('coder', self.gate)] + [(name, gate) for name, gate, _ in ports]
            for name, gate in all_gates:
                base = 'http://127.0.0.1:' + str(gate.server_address[1])
                headers = {'Authorization': 'Bearer EXISTING_TEST_KEY_12345',
                           'Content-Type': 'application/json'}
                with request.urlopen(request.Request(
                        base + '/v1/models', headers=headers), timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(json.load(resp)['data'][0]['id'],
                                     '/models/' + name + '.gguf')
                data = json.dumps({'model': '/models/' + name + '.gguf',
                                   'messages': [{'role': 'user', 'content': 'ping'}],
                                   'stream': False}).encode()
                with request.urlopen(request.Request(
                        base + '/v1/chat/completions',
                        headers=headers, data=data, method='POST'), timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(json.load(resp)['choices'][0]['message']['content'],
                                     '/models/' + name + '.gguf')
                self.assertLessEqual(len(self.driver.loaded), 2)
                # The server may flush its HTTP response before the handler
                # releases the Lease in finally; assert bounded eventual
                # release rather than racing that other thread.
                deadline = __import__('time').monotonic() + 1
                while any(self.coord.active.values()) and __import__('time').monotonic() < deadline:
                    __import__('time').sleep(0.01)
                self.assertTrue(all(value == 0 for value in self.coord.active.values()))
            self.assertEqual(set(self.coord.specs),
                             {'coder', 'qwen3', 'granite', 'ministral'})
        finally:
            for _, gate, worker in ports:
                gate.shutdown()
                gate.server_close()
                worker.join(timeout=1)

    def test_observe_only_get_is_safe_but_post_denied(self):
        self.coord.observe_only=True
        status,data=self.req('/v1/models')
        self.assertEqual(status,200)
        self.assertEqual(data['data'][0]['id'],'/models/coder.gguf')
        status,data=self.req('/v1/chat/completions',
                             {'model':'/models/coder.gguf',
                              'messages':[{'role':'user','content':'ping'}],
                              'stream':False})
        self.assertEqual(status,409)
        self.assertEqual(data['error']['code'],'OBSERVE_ONLY_NO_INFERENCE')
        self.assertEqual(self.driver.ops,[])
        self.assertEqual(self.coord.active['coder'],0)

    def test_authenticated_handoff_http_ticket_and_reclaim(self):
        self.driver.loaded.add('qwen3')
        self.coord.handoff_budget_check=lambda: {
            'selected_memory_ceiling':27, 'selected_cpu_ceiling':6}
        status,_=self.req('/internal/handoff/begin',
                          {'target':'granite','owner':'p'},auth=False)
        self.assertEqual(status,401)
        self.assertEqual(self.driver.ops,[])
        status,data=self.req('/internal/handoff/begin',
                             {'target':'granite','owner':'p','ttl':30})
        self.assertEqual(status,200)
        ticket=data['ticket']
        self.assertEqual(len(self.driver.loaded),3)
        status,data=self.req('/internal/handoff/complete',
                             {'owner':'p','ticket':'wrong'})
        self.assertEqual(status,403)
        self.assertEqual(data['error']['code'],'HANDOFF_TICKET_INVALID')
        self.assertEqual(len(self.driver.loaded),3)
        status,_=self.req('/internal/handoff/complete',
                          {'owner':'p','ticket':ticket})
        self.assertEqual(status,200)
        self.assertEqual(self.driver.loaded,{'granite','qwen3'})

    def test_handoff_status_docker_failure_returns_503_without_killing_gate(self):
        original=self.coord.running
        def unavailable():
            raise AdmissionError(503,'EXISTING_CONTAINER_COMMAND_FAILED')
        self.coord.running=unavailable
        try:
            status,data=self.req('/internal/handoff/status')
            self.assertEqual(status,503)
            self.assertEqual(data['error']['code'],
                             'EXISTING_CONTAINER_COMMAND_FAILED')
        finally:
            self.coord.running=original
        status,data=self.req('/internal/handoff/status')
        self.assertEqual(status,200)
        self.assertEqual(data['resident'],['coder'])

    def test_handoff_status_is_authenticated_and_private(self):
        self.driver.loaded.add('qwen3')
        self.coord.handoff_budget_check=lambda: {'selected_memory_ceiling':27, 'selected_cpu_ceiling':6}
        status,created=self.req('/internal/handoff/begin',{'target':'granite','owner':'p'})
        self.assertEqual(status,200)
        status,view=self.req('/internal/handoff/status')
        self.assertEqual(status,200)
        self.assertNotIn(created['ticket'],json.dumps(view))
        self.assertEqual(len(view['resident']),3)
        status,_=self.req('/internal/handoff/status',auth=False)
        self.assertEqual(status,401)
        status,_=self.req('/internal/handoff/abort',{'owner':'p','ticket':created['ticket']})
        self.assertEqual(status,200)

    def test_status_docker_read_does_not_hold_lease_lock(self):
        started=threading.Event()
        proceed=threading.Event()
        baseline=self.driver.running
        def slow(spec):
            started.set()
            if not proceed.wait(3):
                raise RuntimeError('inspect held too long')
            return baseline(spec)
        self.driver.running=slow
        result=[]
        worker=threading.Thread(
            target=lambda: result.append(self.req('/internal/handoff/status')),
            daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            # Monitoring must not serialize lease state queries against
            # slow Docker inspect. We only read the lock-protected lease.
            with self.coord.lock:
                self.assertEqual(self.coord.active['coder'],0)
        finally:
            proceed.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0],200)

    def test_observe_only_disallows_handoff_http(self):
        self.coord.observe_only=True
        status,data=self.req('/internal/handoff/begin',
                             {'target':'granite','owner':'p'})
        self.assertEqual(status,409)
        self.assertEqual(data['error']['code'],'OBSERVE_ONLY_NO_HANDOFF')
        self.assertEqual(self.driver.ops,[])

    def test_other_model_and_invalid_stream_rejected(self):
        for body in ({'model':'/models/wrong.gguf'},
                     {'model':'/models/coder.gguf','stream':'yes'}):
            status,_=self.req('/v1/chat/completions',body)
            self.assertEqual(status,422)
        self.assertEqual(self.driver.ops,[])

    def test_stream_sse_under_shared_lease(self):
        payload={'model':'/models/coder.gguf','stream':True,
                 'messages':[{'role':'user','content':'ping'}]}
        req=request.Request(self.base+'/v1/chat/completions',
            method='POST',data=json.dumps(payload).encode(),
            headers={'Authorization':'Bearer EXISTING_TEST_KEY_12345',
                     'Content-Type':'application/json'})
        with request.urlopen(req,timeout=3) as resp:
            self.assertEqual(resp.status,200)
            self.assertEqual(resp.headers.get_content_type(),'text/event-stream')
            stream=resp.read()
        self.assertIn(b'data: [DONE]',stream)
        self.assertEqual(self.coord.active['coder'],0)


if __name__=='__main__':
    unittest.main()
