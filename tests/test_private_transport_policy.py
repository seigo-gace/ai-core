"""Validate opt-in private llama backend Compose policy without Host changes."""
import tempfile
from pathlib import Path
import unittest
import yaml
from runtime.model_gate import load_policy, model_specs_from_compose

ROOT = Path(__file__).resolve().parents[1]


class PrivateTransportPolicyTests(unittest.TestCase):
    def test_socket_mode_requires_no_published_tcp(self):
        cfg = yaml.safe_load((ROOT / 'compose.runtime.yml').read_text())
        model = 'coder'
        svc = cfg['services'][model]
        args = svc['command']
        args[args.index('--host') + 1] = '/run/ai-core-backend/coder.sock'
        svc['ports'] = []
        svc['volumes'].append('ai-core-backend-bus:/run/ai-core-backend')
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'compose.yaml'
            file.write_text(yaml.safe_dump(cfg))
            specs = model_specs_from_compose(
                file, load_policy(ROOT / 'runtime/model-policy.yaml'))
            self.assertEqual(specs[model]['backend_socket'],
                             '/run/ai-core-backend/coder.sock')
            svc['ports'] = ['127.0.0.1:18084:8080']
            file.write_text(yaml.safe_dump(cfg))
            with self.assertRaisesRegex(ValueError, 'BACKEND_UNIX_TCP_PUBLISHED'):
                model_specs_from_compose(
                    file, load_policy(ROOT / 'runtime/model-policy.yaml'))
