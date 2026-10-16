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

## 2026-10-13 — Native ChatGPT Host route and two-stage migration

Existing server-core Rule Plane/Work Routine §6 and §9 select registered commands first, then existing `host.command`, then `dev.job` for isolated tests. Fresh normal CHAT native Host receipt [Issue #10 result](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6143098225), from [Actions run](https://github.com/G-ACE-inc/server-core/actions/runs/38380565888), confirms `host.command PASS`, exit 0, exact Host SHA `2b76da06...`, registered source checkout SHA `2955d728...` and four running containers, all `ai-core.slice`, each 9GiB cap. This is real read-only Host evidence, not a deploy.

The 2026-10-13 09:59 UTC Master host inventory reported four idle/current model cgroup counters: total model cgroup `memory.current` **33.30GiB**, selected warm Coder+Qwen3 **15.94GiB**, selected cold Granite+Ministral **17.37GiB**; parent high 34GiB and max 36GiB. The figures are **point-in-time readings**, not measured reclaim or performance evidence; some model cgroup counters may not be disjoint from other components, so do not equate these with total parent consumption. `memory.events.max` values are cumulative, not evidence of new max events during this request; OOM and swap for models reported zero.

**Stage A — OBSERVE-ONLY / non-evicting API compatibility:** `runtime/model_gate.py --serve --observe-only` permits four existing resident backends without automatically starting or stopping any. It uses the same shared Bearer credential, model routing and global request gate. A stopped model returns `OBSERVE_ONLY_MODEL_NOT_RUNNING` without triggering its lifecycle. Stage A now responds to authenticated /v1/models GET and /health but explicitly rejects all POST inference with OBSERVE_ONLY_NO_INFERENCE (HTTP 409). It makes NO model lifecycle changes and cannot compete with legacy -np 1 inference users. Starting any Host daemon still needs existing admitted service/runtime authorization; Host network topology alone is not a production rollout. Do not start it merely because Source tests passed.

**Stage B — EXCLUSIVE RESIDENCY:** `--serve --exclusive-traffic` retains the max-2 residency requirement and can evict only registered no-longer-leased models. It must not be enabled until all legacy direct/proxy consumers have migrated or have an independently proven protected exclusion. The flag alone is not proof or permission. Existing 18080 proxy stays available until the owner confirms cutover.

Current revised Source branch needs exact-SHA tests, including the observe-only protection tests. No Host production model was stopped, no external ports/tokens added, and no provider calls made.

## 2026-10-13 — Existing Host topology and bounded readback evidence

Native normal CHAT Actions/Host receipts: [four container cgroup guard PASS](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6143098225), [Docker network topology PASS](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6143267498), [four direct API /health checks PASS](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6143294349). DebugAI and existing ai-llama-router have host network mode; all four backend containers have separate 127.0.0.1:18081–18084 port binding and report health ok. This confirms local connectivity, **not** newly implemented 18181–18184 gate runtime or multi-product exposure.

Source-exact tests of newer SHA after the 38-test PASS have **not** run: protected Host checkout Git object write [denied](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6143197419), git clone into /tmp [denied](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6143218518), Python tarball extract into temporary directory [denied](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6143374759); an intervening [Host SHA mismatch](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6143354045) was correctly rejected. These are Host safety boundaries, not evidence of code defects. Do not adjust protected permissions solely to bypass them.

Stage A is intentionally a **probe-only** readiness contract, not traffic shadow-inference: /health and authorized /v1/models are exposed, POST gets HTTP 409. Stage B is an exclusive legacy-traffic cutover after separate ownership evidence. Next stage needs isolated source-exact CI/dev.job or equivalent permitted runner; PR remains Draft and must not deploy to live AI Core on static tests alone.

## 2026-10-13 19:29+ JST — Cold-load lock contention repaired

Further source audit revealed that the original `Coordinator.acquire()` held `RLock` during up to 180 seconds of backend startup/readiness checks. That serialized unrelated ready-model requests behind a cold model, defeating the two-model concurrency goal. The owning Source branch now reserves one cold transition, performs fast eviction under the mutex, then releases the global lock during Docker start/health wait. Warm resident models remain available. A second cold transition is rejected while pending; reaper pauses during the transition; rollback restores pretransition residents and releases the reservation on all paths.

Added two threaded regression test methods (warm request during cold model readiness; second cold transition blocked) to `tests/test_model_gate.py`; kept existing rollback, cgroup, identity and per-model concurrency tests. Current suite is 31 new tests + 13 legacy router tests = 44 total definitions, **NOT verified at this new exact SHA**.

ChatGPT reloaded the server-core Manifest (GPT_CHAT), Work Routine §6/§9, #10 terminal Current and ai-core Issue #1. New normal-CHAT native Host request was attempted to run an immutable GitHub archive compile and observe-only POST 409 check entirely in memory (no temp files). It initially did not return before Actions polling ended; Issue #10 later showed it was rejected by valid SOURCE_SHA_MISMATCH after another owner's Host HEAD advanced. A second request on refreshed Host SHA was submitted and its correlated receipt must be inspected separately. This is not source test PASS.

Current production remains four healthy backends with all `ai-core.slice` limits intact. Migration Stage B and DebugAI E2E remain NOT_RUN. No new workflow, MCP, token, project or extra public ports.

## 2026-10-13 — Generic model registry correction / in-memory exact-source proof

- Root defect: Host readiness was hardcoded to the four names `granite`, `qwen3`, `ministral`, `coder`, contradicting the multi-environment one-model/one-API purpose. `runtime/host_readiness.py` now derives its model inventory from `model_specs_from_compose` and the loaded policy, rather than requiring these names; blocking wording now says `RESIDENT_COUNT_EXCEEDS_POLICY` instead of assuming four. Existing four-backend Compose stays unchanged.
- Added `test_non_four_model_registration_is_read_from_policy` to exercise an independently registered `research_model` without operating Docker; the original four-model behavior and safe UNKNOWN traffic flags remain covered.
- Actual exact-HEAD common Host validation: [server-core Actions run 38388887666](https://github.com/G-ACE-inc/server-core/actions/runs/38388887666) → [Host result #6144306913](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6144306913). `host.command` PASS exit0 at fixed Host SHA `50e4ae9f9dc6250a06e8c83cd7f7ce9b1ee6bcd4`. The immutable AI Core source SHA `c96266e77839b4a9aa04bf4fa80b2c60723f0ee7` was fetched and **all 16 tracked Python source files were compiled in memory**; generic one-model fake-Docker inventory reported `AI_CORE_GENERIC_REGISTRY=PASS`. No filesystem writes, Docker operations, or model inference. This **is not** a full unittest suite or production 2-resident acceptance.
- The current source has 27 model-gate test methods + 5 host-readiness test methods + 13 legacy-router tests = **45 test definitions**. Full 45 source-exact unittest execution remains NOT_VERIFIED at this SHA: the Host Writer cannot create test checkouts/temp files by its protected permission design. Preserve those permissions, and use an already-authorized isolated execution route for full tests.

Stage A remains probe-only, not inference or lifecycle mutation. Stage B still requires proven legacy traffic exclusivity and live memory/cold-start E2E.

## 2026-10-13 20:42 JST — Model maintenance lock contention fixed

- Exact original issue: `Coordinator.reap()` held the same RLock used by `Coordinator.acquire()` while issuing synchronous Docker stop/start. A slow *background* cold-idle unload or warm-model restart therefore blocked an otherwise healthy, ready model's request, contradicting the two-model operation goal.
- Source on PR #3 now reserves a single `transitioning` model under the shared lock, releases the lock across Docker stop/start, then releases the transition reservation in `finally`. Other ready models may continue to acquire leases; a request for the transitioning model fails closed with `MODEL_TRANSITION_BUSY`. The existing 2-resident bound and no-stop-of-active-model rule remain unchanged. No change to existing Docker Compose, model files, CPU/RAM quotas or Host deployment.
- Focused regression tests were added for **blocked idle unload / concurrent ready Coder inference**, and for **blocked warm Qwen3 recovery / concurrent Coder inference**, including transition cleanup and final resident identities. Total source test definitions now 34 new (29 model + 5 host) and 13 original router tests = **47 definitions**, not a claim of 47 PASS.
- Exact Source behavioral proof from the existing CHAT→server-core Actions→Host pathway: [Actions run 38391933579](https://github.com/G-ACE-inc/server-core/actions/runs/38391933579), [Issue #10 receipt 6144740198](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6144740198), `host.command STATE=PASS EXIT_CODE=0`, `AI_CORE_REAPER_PARALLEL=PASS SHA=cefa8161ca3cbaece3953ef2806f96c26abf534b`. This used immutable Source loaded in memory and a fake Docker driver with thread synchronization; it did **not** start/stop real containers and is narrower than the full unittest suite.
- Dev-job policy's admitted ai-core checkout is `/home/admin1/projects/ai-core/.worktrees/source-import-20261013` on `import/server-baseline-20261013`. That registered baseline does not contain the current PR's changes. Never report a baseline `dev.job` test as a Source-exact feature test. Reuse registered writable runner when appropriate; do not alter the protected canonical checkout, grant, or source-reflection policy simply to get a test PASS.
- **Current acceptance:** Source modification and targeted Host proof PASS; full 47/47, Stage A real process launch and authenticated probe, Stage B cold-start/cgroup delta/reclaim, DebugAI six-role and second-product E2E remain NOT_VERIFIED. PR must remain DRAFT until actual acceptance.


## 2026-10-14 — Existing four-model API consolidation (current Source, replaces stale introductory examples)

This is a **generic AI Core interface**, not a DebugAI-only feature. Existing four individually addressed model gates are preserved. They are not independent resource managers: all four share **one** Coordinator, one admission/lease policy and the existing backend containers. The design authority is the adopted [generic Core design](https://github.com/seigo-gace/ai-core/blob/main/docs/GENERIC_AI_CORE_DESIGN_VALIDATION_20261014.md). The existing initial warm-pair narrative and early “non-stream only” text above are historical: current model-policy YAML sets all four `warm: false`, and current Gate Source implements SSE streaming (Host verification pending).

| Model alias | Gate (localhost only) | Existing backend (localhost only) | OpenAI-compatible routes |
| --- | --- | --- | --- |
| granite | 127.0.0.1:18181 | 127.0.0.1:18081 | authenticated `GET /v1/models`, `POST /v1/chat/completions` |
| qwen3 | 127.0.0.1:18182 | 127.0.0.1:18082 | same |
| ministral | 127.0.0.1:18183 | 127.0.0.1:18083 | same |
| coder | 127.0.0.1:18184 | 127.0.0.1:18084 | same |

Each model alias is resolved to its **actual GGUF model ID from existing Compose**, not a fabricated replacement identifier. `GET /health` is Gate liveness, not a promise that the model weight is loaded. The existing `LLAMA_SWAP_API_KEY` bearer is reused; no new credential. Model-specific requests require the exact model ID (or the preexisting model alias prefix form). Streaming stays on the same shared Lease while forwarding and must not be confused with demonstrated production compatibility.

**Source/API changes in this CU:** `tests/test_model_gate.py::GateHttpTests.test_four_existing_model_apis_share_one_coordinator` now exercises all four authenticated model endpoints with one fake backend and shared Coordinator, checking per-model IDs, inference forwarding, resource limit ≤2 residents and release of the Lease after each request. `tests/test_generic_model_registry.py` checks non-DebugAI model names and configurable resident policy. The handoff deadline regression tests that transfer TTL begins after target readiness. These are **regression test source added, not executed test PASS** at this feature HEAD.

**Integration order:** 1. Source-exact tests in an approved isolated checkout. 2. Safe Stage A metadata-only Gate startup (never claim probe POST works). 3. Prove all legacy/direct consumers are safely fenced and the 18080 Router-to-Gate auth + SSE contract works. 4. Admitted Stage B activation of the existing four Gate API addresses with two dynamic resident slots, then perform measured 2→3→2 and independent multi-product E2E. No direct model container start/stop or traffic cutover is permitted merely because four Gate listeners exist in Source. The 18080 Router remains the backward-compatible entrypoint until evidence permits transition.

**Remaining generic improvements:** bounded multi-product queue/fairness, restart reconciliation, timeout/cancel/stream termination safety, model-specific profile and runtime OSS feature comparison. These are future Source changes in the same implementation PR, not permission to invent new APIs/ports/workflows.

**Idle recovery correction (current PR #3):** a backend already READY before Gate/Coordinator startup now enters the same idle-timer policy as a backend loaded by that Coordinator. Non-observe-only preflight records the first inherited-resident timestamp; active leases refresh it on release. `--observe-only` never schedules model stopping. Regression cases in `tests/test_generic_model_registry.py` exercise both paths. This is source implementation evidence only until exact-SHA tests run.
