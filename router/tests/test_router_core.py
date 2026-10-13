import subprocess, unittest
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
class T(unittest.TestCase):
  def test_01_only_router_service(self):
    c=yaml.safe_load((ROOT/'docker-compose.llama-swap.yml').read_text()); self.assertEqual(set(c['services']),{'llama-swap'})
  def test_02_runtime_code_has_no_ai_roles_or_tgserver_vector(self):
    paths=list((ROOT/'scripts').glob('*'))+[ROOT/'docker-compose.llama-swap.yml']
    text='\n'.join(p.read_text(errors='ignore') for p in paths if p.is_file())
    for s in ['Evidence Reader','Diagnoser','evidence-reader','patch-engineer','P004','vector_tgserver','prepare_tgserver','TGS_ROOT']:
      self.assertNotIn(s,text)
  def test_03_preflight_contract_static(self):
    p=(ROOT/'scripts/preflight.py').read_text(); self.assertIn("role selectors forbidden",(ROOT/'scripts/runtime_contract_gate.py').read_text()); self.assertIn("globalConcurrencyLimit",p)
  def test_04_backend_override_cgroup_only(self):
    p=(ROOT/'scripts/prepare_backend_overrides.py').read_text(); self.assertIn("cgroup_parent",p); self.assertNotIn("syslog",p); self.assertNotIn("'logging'",p)
  def test_05_install_no_tgserver_vector(self):
    p=(ROOT/'scripts/install_server.sh').read_text(); self.assertNotIn('TGS_ROOT',p); self.assertNotIn('vector',p.lower()); self.assertIn('resource_ceiling_gate.py',p)
  def test_06_compose_router_limits(self):
    s=yaml.safe_load((ROOT/'docker-compose.llama-swap.yml').read_text())['services']['llama-swap']; self.assertEqual(s['cpus'],'0.50'); self.assertEqual(s['mem_limit'],'256m'); self.assertEqual(s['cgroup_parent'],'ai-core.slice')
  def test_07_api_key_localhost(self):
    t=(ROOT/'docker-compose.llama-swap.yml').read_text(); self.assertIn('127.0.0.1:18080',t); self.assertIn('LLAMA_SWAP_API_KEY',t)
  def test_08_model_peers_only(self):
    p=(ROOT/'scripts/generate_config.py').read_text();
    for n in ['granite','qwen3','ministral','coder']: self.assertIn("'"+n+"'",p)
    for r in ['evidence-reader','diagnoser','patch-engineer']: self.assertNotIn(r,p)
  def test_09_qwen_option_is_client_side(self):
    p=(ROOT/'scripts/e2e_llama_swap_gate.py').read_text(); self.assertIn('chat_template_kwargs',p); self.assertNotIn('causal-scout',p)
  def test_10_shell_syntax(self):
    for p in ROOT.glob('scripts/*.sh'): subprocess.run(['bash','-n',str(p)],check=True)
  def test_11_config_generation_bans_cloud_targets(self):
    p=(ROOT/'scripts/generate_config.py').read_text()
    for x in ['openai.com','anthropic.com','groq.com','googleapis.com']: self.assertNotIn(x,p)
  def test_12_readonly_collector_no_mutation(self):
    p=(ROOT/'scripts/collect_contabo_readonly_baseline.sh').read_text();
    for x in ['docker compose up','docker compose down','systemctl',' install ',' rm ']: self.assertNotIn(x,p)
  def test_13_resource_policy_is_measured_not_fixed_8cpu_8gb(self):
    p=(ROOT/'scripts/generate_resource_policy.py').read_text(); self.assertNotIn('cpus < 8',p); self.assertNotIn('max(8',p); self.assertIn('0.50',p); self.assertIn('0.60',p)
if __name__=='__main__': unittest.main(verbosity=2)
