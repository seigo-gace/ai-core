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

if __name__=='__main__':
    unittest.main()
