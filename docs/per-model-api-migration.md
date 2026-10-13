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

## Source safety review after initial 28-test PASS (2026-10-13)
- The first exact branch SHA `47e3a785...` passed 15 new and 13 existing tests on the host. This is **source evidence only**, not Host lifecycle or RAM-recovery evidence.
- A subsequent review found that `docker compose -f compose.runtime.yml up -d SERVICE` can recreate a container without the separately installed backend cgroup override. This would weaken the existing protected CPU/RAM boundary. The model gate now validates Docker Compose labels, model ID, local-only port, `CgroupParent=ai-core.slice`, actual Docker Memory/NanoCpus, and only starts/stops **already created** containers with Docker start/stop; it may not recreate them.
- Docker's `9216m` matches `9216 * 1024**2` bytes in the observed Host configuration, not decimal `9216 * 1000**2`. Memory comparison corrected. Each `-np 1` backend now allows at most one simultaneous leased inference.
- The guard changes and added tests on this feature branch **still require fresh exact-HEAD source tests**; prior 28-test PASS does not transfer to the revised code. Use `bash scripts/verify-model-gate-source.sh` in a clean isolated checkout of the actual branch head. This script does not operate Docker.
- Until verified cutover ownership, the old four resident containers and any unleased traffic must remain untouched. PR stays Draft; production acceptance is **NOT_VERIFIED**.

## 2026-10-13 — Source-exact 33/33 PASS and Host inspection step

The Master returned an unmodified envelope for HEAD 6c39fb84966c1c2cc751afbd1d5e464386f4040c:
new 20/20, existing router 13/13, preflight 4 models PASS, VERIFY=PASS.
This is a verified SOURCE gate, not live host eviction or memory reclamation.

Additional source review corrected a partial-failure path: if Docker stops a resident but its stop acknowledgment fails, restore the full pre-transition resident set, not only the already-reported eviction list. A regression test now covers that failure and verifies the semaphore releases.

The new runtime/host_readiness.py is an **explicitly read-only** Host inventory, using source-locked model identities and resource controls. It reads Docker inspect/cgroup/instance information without Docker mutations, token values or full container environment. It reports:
- Current number of running models versus policy max_resident.
- Each existing container's Compose identity, model, cgroup parent, CPU/RAM limits and loopback port.
- Parent cgroup CPU/RAM ceiling and available model memory/swap counters where accessible.
- Both legacy traffic and exclusive-consumer ownership as UNKNOWN, even when only two models are running.

A four-resident production Host must report blocked for two-resident cutover without stopping any model. A source verifier or Host inventory PASS is not an authorization to run gate --serve. No new CI workflow has been introduced.

**NEXT**: source-exact new test suite (now 25 new methods + existing 13) and Host read-only inventory from the existing in-parent checkout; then fix mismatches if any, and separately qualify traffic ownership before admitted live transition.
