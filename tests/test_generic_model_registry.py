import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from runtime.model_gate import Coordinator
from test_model_gate import FakeDriver


def custom_specs():
    return {
        name: {
            "service": name, "model_id": "/models/" + name + ".gguf",
            "backend_port": 23000 + i, "gate_port": 24000 + i,
            "warm": False, "memory_limit_bytes": 1024 * 1024 * 1024,
            "cpu_limit": 1.0,
        }
        for i, name in enumerate(("alpha", "beta", "gamma"))
    }


class GenericModelRegistryTests(unittest.TestCase):
    def test_dynamic_registry_not_tied_to_debugai_models(self):
        entries = custom_specs()
        driver = FakeDriver(("alpha",))
        control = Coordinator(entries, driver, max_loaded=2, max_active=2)
        with control.lease("beta"):
            self.assertEqual(driver.loaded, {"alpha", "beta"})
        with control.lease("gamma"):
            self.assertLessEqual(len(driver.loaded), 2)
            self.assertIn("gamma", driver.loaded)

    def test_separate_policy_limits_are_not_hardcoded(self):
        entries = custom_specs()
        driver = FakeDriver()
        control = Coordinator(entries, driver, max_loaded=1, max_active=1)
        with control.lease("gamma"):
            self.assertEqual(driver.loaded, {"gamma"})
        self.assertLessEqual(len(driver.loaded), control.max_loaded)


if __name__ == "__main__":
    unittest.main()
