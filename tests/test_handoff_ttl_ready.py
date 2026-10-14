import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from runtime.model_gate import Coordinator
from test_model_gate import FakeDriver, specs


class TestHandoffReadyDeadline(unittest.TestCase):
    def test_transfer_ttl_starts_after_cold_readiness(self):
        now = [100.0]
        coordinator = [None]

        class SlowReady(FakeDriver):
            def healthy(self, spec):
                # The model is only READY at t=250, after a 10-second transfer TTL.
                now[0] = 250.0
                self_test.assertFalse(coordinator[0].handoff_expire())
                return True

        self_test = self
        driver = SlowReady(('coder', 'qwen3'))
        coordinator[0] = Coordinator(
            specs(), driver, clock=lambda: now[0],
            handoff_budget_check=lambda: {
                'selected_memory_ceiling': 27, 'selected_cpu_ceiling': 6,
            },
        )
        state = coordinator[0].handoff_begin(
            'coder', 'ministral', 'project-a', ttl=10,
        )
        self.assertEqual(state['expires'], 260.0)
        self.assertEqual(driver.loaded, {'coder', 'qwen3', 'ministral'})
        now[0] = 259.0
        self.assertFalse(coordinator[0].handoff_expire())
        now[0] = 261.0
        self.assertTrue(coordinator[0].handoff_expire())
        self.assertEqual(driver.loaded, {'coder', 'qwen3'})


    def test_inherited_ready_resident_gets_idle_deadline(self):
        now = [100.0]
        driver = FakeDriver(('coder',))
        coordinator = Coordinator(
            specs(), driver, idle_seconds=10, clock=lambda: now[0],
        )
        self.assertEqual(coordinator.preflight(), {'coder'})
        self.assertEqual(coordinator.used['coder'], 100.0)
        now[0] = 109.0
        coordinator.reap()
        self.assertEqual(driver.loaded, {'coder'})
        now[0] = 111.0
        coordinator.reap()
        self.assertEqual(driver.loaded, set())



if __name__ == '__main__':
    unittest.main()
