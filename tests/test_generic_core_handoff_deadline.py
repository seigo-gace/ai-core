import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from runtime.model_gate import Coordinator
from test_model_gate import FakeDriver, specs


class GenericCoreHandoffDeadlineTests(unittest.TestCase):
    def test_startup_does_not_consume_transfer_ttl(self):
        now = [100.0]
        coordinator = [None]

        class DelayedDriver(FakeDriver):
            def healthy(self, spec):
                now[0] = 250.0
                self_test.assertFalse(coordinator[0].handoff_expire())
                return True

        self_test = self
        driver = DelayedDriver(("coder", "qwen3"))
        coordinator[0] = Coordinator(
            specs(), driver, clock=lambda: now[0],
            handoff_budget_check=lambda: {
                "selected_memory_ceiling": 27, "selected_cpu_ceiling": 6
            },
        )
        state = coordinator[0].handoff_begin(
            "coder", "ministral", "tenant-one", ttl=10
        )
        self.assertEqual(state["expires"], 260.0)
        self.assertFalse(coordinator[0].handoff_expire())
        now[0] = 261.0
        self.assertTrue(coordinator[0].handoff_expire())
        self.assertEqual(driver.loaded, {"coder", "qwen3"})


if __name__ == "__main__":
    unittest.main()
