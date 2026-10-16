'use strict';
// AI Core integration acceptance only; never product Source.
const fs = require('node:fs');
const {fetchJson} = require('/app/src/llm/http-client.js');
const OpenAICompatAdapter = require('/app/src/llm/providers/openai-compatible.js');
async function main() {
  let key = fs.readFileSync(0, 'utf8').trim();
  if (key.length < 16 || key.length > 4096) throw new Error('EXISTING_GATE_AUTH_UNAVAILABLE');
  const legacy = process.argv[3] === 'legacy';
  if (process.argv[3] && !legacy) throw new Error('INVALID_GATE_SELECT');
  const gatePort = legacy ? 18080 : 18181;
  const {res,data} = await fetchJson('http://127.0.0.1:' + gatePort + '/v1/models', {
    method: 'GET', headers: {Authorization: 'Bearer ' + key}, timeoutMs: 10000
  });
  const authenticated = res.status === 200 &&
    Array.isArray(data.data) && data.data.length === (legacy ? 4 : 1) &&
    typeof data.data[0]?.id === 'string' && data.data[0].id.length > 0;
  const mode = process.argv[2] || 'catalogue';
  if (mode !== 'catalogue' && mode !== 'inference') {
    throw new Error('INVALID_PROBE_MODE');
  }
  if (!authenticated) {
    process.stdout.write(JSON.stringify({
      probe: 'AI_CORE_SECOND_PRODUCT_ASTERA_V8', mode,
      http_status: res.status, authenticated: false, inference: 'NOT_RUN'
    }) + '\n');
    process.exitCode = 1;
    return;
  }
  if (mode === 'catalogue') {
    key = '';
    process.stdout.write(JSON.stringify({
      probe: 'AI_CORE_SECOND_PRODUCT_ASTERA_V8', mode,
      runtime: 'existing-astera-v8-node', gate: 'granite',
      http_status: res.status, model_count: data.data.length,
      authenticated: true, inference: 'NOT_RUN', product_source_changed: false
    }) + '\n');
    return;
  }
  // Use the exact existing Astera v8 product adapter (not a hand-built POST).
  // Options are confined to this short-lived process, never its product config.
  const adapter = new OpenAICompatAdapter({
    baseUrl: 'http://127.0.0.1:' + gatePort,
    apiKey: key,
    model: data.data[0].id
  });
  const started = process.hrtime.bigint();
  const result = await adapter.generate('Reply with exactly OK.');
  const durationMs = Number((process.hrtime.bigint() - started) / 1000000n);
  key = '';
  const answerValid = result?.provider === 'compat' &&
    result.model === data.data[0].id &&
    typeof result.text === 'string' && result.text.trim().length > 0 &&
    Array.isArray(result.raw?.choices) && result.raw.choices.length > 0;
  process.stdout.write(JSON.stringify({
    probe: 'AI_CORE_SECOND_PRODUCT_ASTERA_V8', mode,
    runtime: 'existing-astera-v8-node', adapter: 'OpenAICompatAdapter',
    gate: legacy ? 'compat-18080' : 'granite', authenticated: true,
    http_status: res.status,
    model_count: data.data.length,
    inference: answerValid ? 'PASS' : 'FAIL',
    duration_ms: durationMs,
    answer_nonempty: answerValid,
    usage_tokens: Number.isFinite(result.raw?.usage?.total_tokens) ?
      result.raw.usage.total_tokens : null,
    product_source_changed: false
  }) + '\n');
  if (!answerValid) process.exitCode = 1;
}
main().catch(error => {
  console.error('AI_CORE_SECOND_PRODUCT_PROBE_FAILURE=' +
                (error.name === 'Error' ? error.message : error.name));
  process.exitCode = 1;
});
