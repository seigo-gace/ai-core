'use strict';
// AI Core integration acceptance only; never product Source.
const fs = require('node:fs');
const {fetchJson} = require('/app/src/llm/http-client.js');
async function main() {
  let key = fs.readFileSync(0, 'utf8').trim();
  if (key.length < 16 || key.length > 4096) throw new Error('EXISTING_GATE_AUTH_UNAVAILABLE');
  const {res,data} = await fetchJson('http://127.0.0.1:18181/v1/models', {
    method: 'GET', headers: {Authorization: 'Bearer ' + key}, timeoutMs: 10000
  });
  key = '';
  const passed = res.status === 200 && Array.isArray(data.data) && data.data.length === 1;
  process.stdout.write(JSON.stringify({
    probe: 'AI_CORE_SECOND_PRODUCT_ASTERA_V8',
    runtime: 'existing-astera-v8-node', gate: 'granite',
    http_status: res.status, model_count: data.data?.length ?? 0,
    authenticated: passed, inference: 'NOT_RUN', product_source_changed: false
  }) + '\n');
  if (!passed) process.exitCode = 1;
}
main().catch(error => {
  console.error('AI_CORE_SECOND_PRODUCT_PROBE_FAILURE=' +
                (error.name === 'Error' ? error.message : error.name));
  process.exitCode = 1;
});
