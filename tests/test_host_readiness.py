import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from host_readiness import report, MODELS
from model_gate import load_policy


class ReadOnlyDriver:
    def __init__(self, running=(), broken=()):
        self.running_names=set(running)
        self.broken=set(broken)
        self.ops=[]
    def _inspect(self, spec):
        self.ops.append(('inspect',spec['service']))
        if spec['service'] in self.broken:
            from model_gate import AdmissionError
            raise AdmissionError(409,'BACKEND_CGROUP_NOT_PROTECTED')
        return {'State':{'Running':spec['service'] in self.running_names,'Pid':0}}


class TestHostReadiness(unittest.TestCase):
    def setUp(self):
        self.policy=load_policy(ROOT/'runtime/model-policy.yaml')
        self.budget={
            'parent_memory_max':36*1024**3,'parent_memory_high':34*1024**3,
            'parent_cpu_cores':6,'selected_memory_ceiling':18*1024**3,
            'selected_cpu_ceiling':6}
    def run_probe(self, driver):
        return report(ROOT/'compose.runtime.yml',self.policy,driver=driver,
                      parent_budget=self.budget)
    def test_four_running_blocks_without_mutation(self):
        driver=ReadOnlyDriver(MODELS)
        output=self.run_probe(driver)
        self.assertEqual(output['running_backends'],4)
        self.assertEqual(output['admission'],'BLOCKED')
        self.assertEqual(output['consumer_exclusivity'],'UNKNOWN')
        self.assertFalse(output['production_cutover_approved'])
        self.assertEqual(len(driver.ops),4)
        self.assertTrue(all(op=='inspect' for op,_ in driver.ops))
    def test_two_running_still_does_not_prove_traffic_exclusivity(self):
        out=self.run_probe(ReadOnlyDriver(('coder','qwen3')))
        self.assertEqual(out['admission'],'SOURCE_AND_HOST_ONLY')
        self.assertEqual(out['consumer_exclusivity'],'UNKNOWN')
        self.assertFalse(out['production_cutover_approved'])
    def test_cgroup_or_owner_mismatch_is_visible(self):
        out=self.run_probe(ReadOnlyDriver(('coder','qwen3'),('ministral',)))
        self.assertEqual(out['admission'],'BLOCKED')
        self.assertEqual(out['backend_contract_failures'],['ministral'])
        bad=next(x for x in out['models'] if x['model']=='ministral')
        self.assertEqual(bad['error'],'BACKEND_CGROUP_NOT_PROTECTED')
    def test_result_has_no_model_weight_secret_or_host_mutation(self):
        out=self.run_probe(ReadOnlyDriver(('coder',)))
        dumped=json.dumps(out)
        self.assertNotIn('LLAMA_SWAP_API_KEY',dumped)
        self.assertNotIn('Authorization',dumped)
        self.assertNotIn('/models/',dumped)
        self.assertEqual(out['action'],'READ_ONLY')
