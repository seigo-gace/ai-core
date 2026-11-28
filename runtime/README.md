# AI Core handoff protocol (implementation PR #3)

Authority: docs/DYNAMIC_RESIDENCY_HANDOFF_DESIGN.md in main.
This file describes a development candidate, not a production deployment.

## Normal operation
- Four backend identities remain addressable; normal resident count <=2.
- Admission leases protect models in use; global active requests <=2.
- Idle backends release memory when safe. The legacy 18080 router is retired: it is never started, proxied through, or used as an operational fallback. All inference requests must acquire a lease through the shared Coordinator and model-specific Gate.

## Bounded inference admission
- Existing requests remain immediately fail-fast by default.
- Requests may opt into a finite waiting queue with
  `X-AI-Core-Wait-Ms: 1..30000`. The shared Coordinator enforces a
  maximum 64 queued requests, FIFO turn-taking, and a monotonic deadline.
- A timed-out request returns 429 `ADMISSION_DEADLINE_EXPIRED`; a full queue
  returns 429 `ADMISSION_QUEUE_FULL`. The queue never owns a model lease
  or inference slot until a request is actually admitted.
- This is a bounded first implementation of queue admission, not proof of
  two-product weighted fairness, cancellation-on-disconnect, or crash recovery.

## Temporary handoff
- Begin: authenticated POST /internal/handoff/begin to the old model's
  loopback gate, body with target, owner and ttl.
- Begin reserves a temporary third resident only after a real cgroup
  memory High/headroom test; owner receives an opaque ticket.
- Complete or abort: authenticated POST /internal/handoff/complete or
  /internal/handoff/abort with owner and the returned ticket.
- Completion stops the former model; abort or TTL expiration stops the
  temporary model. A busy leased model must not be stopped.
- Recovery from Docker failure keeps the reservation and denies new
  cold starts until the third resident has actually stopped.
- Authenticated GET /internal/handoff/status returns resident and active
  counts but never the ticket or the owner.

Owner identifies a caller for auditing; it is not a security principal.
The pre-existing bearer credential authenticates the local API. Tokens
must not be logged. Publicly accessible ports are not commissioned.

## Verification and deployment limits
- Current exact-source tests use FakeDriver and temporary HTTP loopback.
- This does not prove real model RAM reclaim or production readiness.
- Do not start the retired 18080 router. Before stopping any model, prove
  that active callers use the new authenticated Gate and no unleased direct
  backend client is active. Verify applicable Host admission and deployment
  readiness. Reuse existing Compose-owned containers. Do not grant the
  router Docker socket access or increase the 36 GiB/6-core ceilings.

## Qwen3.5-4B qualification candidate (2026-11-28 JST)
This branch adds the fifth *selectable local candidate* (not a fifth normally resident
model) without changing the four established model IDs, sockets, limits, or
consumer roles. The temporary five-model inventory is for qualification only;
DebugAI's final specialist composition remains governed by its own Issue #41.
The candidate gate is loopback 18185, with a private Unix backend and on-demand
loading by the **same Coordinator**. No old router, second Coordinator, public
endpoint, new credential, or provider call. Existing parent CPU6 / High34GiB /
Max36GiB, max resident 2, max active 2, idle 120s and temporary handoff max 3
are unchanged; the model inherits the existing 3-thread/8192-context/9GiB
per-container baseline for measurement, not as an asserted optimum.

Host entry criteria: verify that 18185 is unused, the already downloaded GGUF
matches the recorded checksum, the installed llama.cpp image supports its
architecture/chat template, and existing consumers and lease state are safe.
Create only the candidate backend (stopped) through the existing ai-core Compose
owner. A Gate candidate image must contain both exact Compose and policy; the
old runtime-only Gate refresh and historical four-backend cutover scripts
cannot be reused unchanged to deploy this model. Retain the prior Gate image
and four-model Compose/healthcheck for rollback. Do not deploy until the
transaction, rollback, and source qualification have been verified. The
candidate inference probe checks exact model identity and actual "OK" completion,
rather than only the existence of a message. Cold/warm, JSON, thinking toggle,
tool calls, lease/handoff/reclaim and measured CPU/RAM remain live acceptance
conditions. This does not restart DebugAI's paused 50-question suite.

## DeepSeek external candidate (same Gate, optional overlay)
Use the existing AI Core Gate process and API credential with loopback port
18186. This is an external transport, not a sixth local container, resident
model, queue, or Coordinator. The provider key stays in the existing Libral
Vault. The narrowly scoped AI Core caller file is mounted read-only using
`compose.gate.openrouter.yml` only after provisioning. No caller file means
the remote listener is not enabled. The remote model ID is the pinned
`deepseek/deepseek-v4.1-flash-1017` catalog ID, and actual response identity
must agree; /v1/models is configured identity, not proof of provider access.
The external candidate accepts only explicit SYNTHETIC_QUALIFICATION text
packets, bounded JSON output, distillable-only provider routing, no data
collection, explicit price ceilings ($0.20/M prompt, $1/M completion), no
silent fallback/retry and no raw provider reasoning/error bodies. Keep the
existing account's cumulative $10 key limit. Live key authorization, real
completion, teacher judgment and training are separate acceptance checks.
