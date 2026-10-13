# AI Core per-model APIs — 2026-10-13

## Purpose
One model = one independently addressable local API; four model types stay available, with 2 resident and 2 on demand under the existing CPU/RAM ceiling. DebugAI's six roles are the first acceptance consumer. This does not replace llama.cpp or remove the existing authenticated compatibility router.

## Reuse
The four backend llama.cpp services retain ports 18081–18084 and their existing Compose tuning. Source model gates bind 18181–18184, all 127.0.0.1 only, selected from the Compose-backed registry and the new model-policy YAML. No new model weights, external ports, tokens, workflows, projects, or Docker socket exposure.

Initial policy: max_resident=2, max_active=2, warm=Coded/Qwen3 (actual names: coder and qwen3), cold=granite/ministral. These are testable initial allocations, not measured optimum. The gate reads the approved ai-core.slice memory.max, memory.high and cpu.max; it rejects resident worst-case footprints larger than these and cannot change them. It refuses to evict a leased model; cold models are stopped after their idle TTL and warm residents may be restored.

## Strict migration boundary
This feature branch is **source-only**. Do not run the gate with the existing four-resident production workloads or direct legacy consumers still using old backend ports. It refuses startup if more than the configured resident count are already running and --serve requires --exclusive-traffic. That flag **cannot prove** ownership or authorize production cutover. Before model eviction is enabled, every legacy client must be migrated or isolated, and the existing approved Host transport must be used.

Existing 18080 llama-swap retains authentication, 2-request concurrency, and compatibility until tests verify parity. The new per-model gates require the same LLAMA_SWAP_API_KEY Bearer secret; no new credential. GET /health means gate liveness, not loaded-model readiness; GET /v1/models and POST /v1/chat/completions require auth. Calls support non-stream JSON only and reject wrong models or streaming. Errors during a cold start attempt restoration of evicted warm models; rollback failure is explicit.

## Source validation
- python3 -m unittest discover -s tests -p test_*.py -v
- python3 -m unittest discover -s router/tests -p test_*.py -v
- python3 runtime/model_gate.py --preflight

The new tests use local fake backends and do not start Docker. No Host E2E is claimed. Unverified: actual model cold-start timing, cgroup memory/swap reclaim, client cutover, Docker Compose runner authorization, end-to-end six DebugAI roles and a second consuming product.

## Next CU
First source CI/readback. Then prepare an explicitly admitted, reversible Host rollout preserving legacy router and backends. Benchmark one cold model and then four models under a two-resident policy, including quality, peak RAM, swap, CPU throttling, concurrent requests, restart and rollback. Only afterward switch DebugAI adapter to four independent gates.

GitHub Mission: https://github.com/seigo-gace/ai-core/issues/1
