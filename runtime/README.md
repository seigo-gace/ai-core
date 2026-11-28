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
the remote listener is not enabled. The Gate retains its existing UID and
capability restrictions. Before the overlay is used, verify the dedicated
caller file owner is UID 1000 and determine its actual GID; verify the group
contains only the existing owner (primary and supplementary memberships).
Only after that verification, grant read-only group access (0640) to this
dedicated caller file. Set `AI_CORE_CALLER_FILE_EXPECTED_GID` to the verified
GID in the existing Compose environment. The overlay requires it, adds that
exact group, and checks actual UID/GID/mode on the opened file descriptor.
A 0600 file owned by UID 1000 is not readable by the existing UID 0 Gate
with all capabilities dropped; removing `group_add` does not fix this.
Never grant DAC override or use an admin token. The remote model ID is the pinned
`deepseek/deepseek-v4.1-flash-1017` catalog ID, and actual response identity
must agree; /v1/models is configured identity, not proof of provider access.
The external candidate accepts only explicit SYNTHETIC_QUALIFICATION text
packets, bounded JSON output, distillable-only provider routing, no data
collection, explicit price ceilings ($0.20/M prompt, $1/M completion), no
silent fallback/retry and no raw provider reasoning/error bodies. Keep the
existing account's cumulative $10 key limit. Live key authorization, real
completion, teacher judgment and training are separate acceptance checks.

## Offline Qwen3.5-4B text distillation backend
`runtime/qwen_distillation.py` uses the exact Qwen3.5 text head from its
immutable official Safetensors checkpoint, not the serving GGUF. The owner
injects the authenticated KB authorized/independently accepted batch reader;
there is no public CLI, caller-supplied rights grant, new service or model.
The reader is currently NOT_CONNECTED, so actual KB training remains denied.
Teacher projections from DebugAI are candidates, not permission. Train only
accepted SFT/recovery data, never held-out answers. Verify prompt and output
hashes and the official chat-template completion-only labels; no truncation
or silent substitution. PEFT all-linear LoRA (r16), token-weighted loss,
finite gradient and actual weight-update checks, immutable adapter files and
run receipt. A previous verified adapter can continue on the same base.
Defaults are initial engineering settings, not tuned hyperparameters.
Host CPU <=6 and admitted memory bounds remain authoritative. BF16 is preferred
on a supported accelerator; CPU FP32 is a possible measured fallback, not a
qualified or fast configuration. Actual checkpoint load, optimizer run,
independent quality, GGUF conversion, resource qualification, trusted KB
readback and adoption/rollback remain separate. Never claim training success
from these contract tests. Official sources:
https://huggingface.co/docs/transformers/model_doc/qwen3_5 ;
https://huggingface.co/Qwen/Qwen3.5-4B/raw/851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a/chat_template.jinja ;
https://huggingface.co/docs/peft/main/package_reference/lora .
