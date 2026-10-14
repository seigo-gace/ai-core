"""Regression tests for generic single-resident and already-ready handoff."""
import unittest
from runtime.model_gate import Coordinator

NAMES=('granite','qwen3','ministral','coder')
def specs():
    return {name:{'service':name,'warm':False,'memory_limit_bytes':9216000000,'cpu_limit':3.0}
            for name in NAMES}

class Driver:
    def __init__(self, names):
        self.loaded=set(names)
        self.ops=[]
    def running(self, spec):
        return spec['service'] in self.loaded
    def healthy(self, spec):
        return True
    def start(self, spec):
        self.ops.append(('start',spec['service']))
        self.loaded.add(spec['service'])
    def stop(self, spec):
        self.ops.append(('stop',spec['service']))
        self.loaded.remove(spec['service'])

class GenericHandoffTests(unittest.TestCase):
    def test_ready_target_abort_preserves_both_residents(self):
        driver=Driver(('coder','granite'))
        def budget():
            self.fail('already-ready target requires no new memory reservation')
        core=Coordinator(specs(),driver,handoff_budget_check=budget)
        state=core.handoff_begin('coder','granite','client')
        self.assertEqual(driver.ops,[])
        core.handoff_finish('client',completed=False,ticket=state['ticket'])
        self.assertEqual(driver.loaded,{'coder','granite'})
        self.assertEqual(driver.ops,[])

    def test_one_resident_handoff_reclaims_original(self):
        driver=Driver(('coder',))
        core=Coordinator(specs(),driver,handoff_budget_check=lambda:
                         {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        state=core.handoff_begin('coder','granite','client')
        self.assertEqual(driver.loaded,{'coder','granite'})
        core.handoff_finish('client',ticket=state['ticket'])
        self.assertEqual(driver.loaded,{'granite'})
        self.assertEqual(driver.ops,[('start','granite'),('stop','coder')])

    def test_ready_target_complete_only_reclaims_source(self):
        driver=Driver(('coder','granite'))
        core=Coordinator(specs(),driver,handoff_budget_check=lambda: self.fail('no cold start'))
        state=core.handoff_begin('coder','granite','client')
        core.handoff_finish('client',ticket=state['ticket'])
        self.assertEqual(driver.loaded,{'granite'})
        self.assertEqual(driver.ops,[('stop','coder')])

    def test_single_resident_abort_only_reclaims_new_target(self):
        driver=Driver(('coder',))
        core=Coordinator(specs(),driver,handoff_budget_check=lambda:
                         {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        state=core.handoff_begin('coder','granite','client')
        core.handoff_finish('client',completed=False,ticket=state['ticket'])
        self.assertEqual(driver.loaded,{'coder'})
        self.assertEqual(driver.ops,[('start','granite'),('stop','granite')])

    def test_ready_target_expiry_keeps_preexisting_model(self):
        clock=[0]
        driver=Driver(('coder','granite'))
        core=Coordinator(specs(),driver,clock=lambda:clock[0],
                         handoff_budget_check=lambda: self.fail('no cold start'))
        core.handoff_begin('coder','granite','client',ttl=2)
        clock[0]=3
        self.assertTrue(core.handoff_expire())
        self.assertEqual(driver.loaded,{'coder','granite'})
        self.assertIsNone(core.handoff)

    def test_failed_handoff_rollback_keeps_admission_until_recovered(self):
        from runtime.model_gate import AdmissionError
        class FlakyDriver(Driver):
            def __init__(self, names):
                super().__init__(names)
                self.fail_stop=True
            def healthy(self, spec):
                return False
            def stop(self, spec):
                if self.fail_stop:
                    raise OSError('transient stop failure')
                super().stop(spec)
        clock=[0]
        driver=FlakyDriver(('coder',))
        core=Coordinator(specs(),driver,clock=lambda:clock[0],
                         start_timeout=0,handoff_budget_check=lambda:
                         {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_ROLLBACK_PENDING'):
            core.handoff_begin('coder','granite','client')
        self.assertIsNotNone(core.handoff)
        self.assertEqual(driver.loaded,{'coder','granite'})
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_IN_PROGRESS'):
            core.handoff_begin('coder','ministral','other')
        driver.fail_stop=False
        self.assertTrue(core.handoff_expire())
        self.assertEqual(driver.loaded,{'coder'})
        self.assertIsNone(core.handoff)

    def test_handoff_retry_does_not_stop_already_absent_victim(self):
        driver=Driver(('coder','granite'))
        core=Coordinator(specs(),driver)
        state=core.handoff_begin('coder','granite','client')
        driver.loaded.remove('coder')  # prior stop completed; acknowledgement lost
        core.handoff_finish('client',ticket=state['ticket'])
        self.assertEqual(driver.ops,[])
        self.assertEqual(driver.loaded,{'granite'})
        self.assertIsNone(core.handoff)

    def test_expired_handoff_waits_for_target_lease_before_reclaim(self):
        from runtime.model_gate import AdmissionError
        clock=[0]
        driver=Driver(('coder',))
        core=Coordinator(specs(),driver,clock=lambda:clock[0],
                         handoff_budget_check=lambda:
                         {'selected_memory_ceiling':27,'selected_cpu_ceiling':6})
        state=core.handoff_begin('coder','granite','client',ttl=2)
        clock[0]=3
        with core.lease('granite'):
            with self.assertRaisesRegex(AdmissionError,'HANDOFF_MODEL_LEASED'):
                core.handoff_expire()
            self.assertEqual(driver.loaded,{'coder','granite'})
            self.assertIsNotNone(core.handoff)
        self.assertTrue(core.handoff_expire())
        self.assertEqual(driver.loaded,{'coder'})
        self.assertIsNone(core.handoff)

    def test_busy_source_rejects_completion_without_stopping(self):
        from runtime.model_gate import AdmissionError
        driver=Driver(('coder','granite'))
        core=Coordinator(specs(),driver,handoff_budget_check=lambda: self.fail('no cold start'))
        state=core.handoff_begin('coder','granite','client')
        with core.lease('coder'):
            with self.assertRaisesRegex(AdmissionError,'HANDOFF_MODEL_LEASED'):
                core.handoff_finish('client',ticket=state['ticket'])
            self.assertEqual(driver.ops,[])
            self.assertIsNotNone(core.handoff)
        core.handoff_finish('client',ticket=state['ticket'])
        self.assertEqual(driver.loaded,{'granite'})

    def test_wrong_ticket_never_stops_preexisting_target(self):
        from runtime.model_gate import AdmissionError
        driver=Driver(('coder','granite'))
        core=Coordinator(specs(),driver,handoff_budget_check=lambda: self.fail('no cold start'))
        core.handoff_begin('coder','granite','client')
        with self.assertRaisesRegex(AdmissionError,'HANDOFF_TICKET_INVALID'):
            core.handoff_finish('client',completed=False,ticket='bad')
        self.assertEqual(driver.ops,[])
        self.assertIsNotNone(core.handoff)

if __name__=='__main__':
    unittest.main()
