# AI Core — G-ACE shared local inference runtime

**Source owner:** this repository. **Current runtime source:** `/home/admin1/projects/ai-core` on the existing host until a verified, reversible migration is completed.

## Design authority

**AI Core's adopted design (after the owning design PR is merged to main):** [Dynamic residency, temporary three-model handoff and shared resource reuse](docs/DYNAMIC_RESIDENCY_HANDOFF_DESIGN.md). The document fixes the Master-approved 36 GiB / 6-core ceiling, normally 0–2 variable resident models, and only during bounded handoff up to 3 residents, followed by returning to <=2. The older “two initial warm models” and “always at most two” phrasing below is historical bootstrap context, not superseding authority. Implementation PR #3 must be checked against this design before production cutover.

## Goal

Make **one model = one independently addressable local API**, reusing the existing four llama.cpp backends instead of forcing every AI product through one four-model endpoint. Preserve the **host-wide AI CPU/RAM budget**, model-level resource ceilings, measured tuning, cgroup v2 protection, validation and rollback. **DebugAI's first acceptance case is four model types, six roles, but only two resident models** with the others available on demand. Do not equate independent APIs with automatic RAM release.

## Import boundary

The original implementation exists on the host and must be imported from there, not recreated from partial chat snippets. Use the checked-in `scripts/import-host-source.sh` on an isolated branch in a nested checkout **inside** the existing `/home/admin1/projects/ai-core` parent. The importer uses an explicit source-file allowlist and excludes secrets, model weights, generated host configuration, runtime evidence and host logs.

**Never commit:** `.env*`, API keys, `models/`, `*.gguf`, `runtime-authority/`, `router/generated/`, `router/config/config.generated.yaml`, `router/evidence/` except the public llama-swap version contract, or any live server secret.

## Existing contracts to preserve

- Independent backend listeners already exist for Granite, Qwen3, Ministral and Coder (loopback ports 18081–18084).
- The current llama-swap 18080 endpoint is an authenticated compatibility proxy with a total concurrency limit; bypassing it must **not bypass security/admission**.
- `router/scripts/runtime_contract_gate.py` currently requires `models: {}` and forbids the **router** from owning the backend lifecycle. Keep that boundary unless intentionally redesigned and validated.
- Existing `ai-core.slice`, `compose.runtime.yml`, generated resource plan, overrides, actual-value gates and rollback are source assets to reuse.
- Current host resource authority and production state are **not** changed by bootstrapping this repository.

## Source workflow

1. Import the real source on a dedicated branch; run existing focused tests and check the full import diff for accidental sensitive material.
2. Improve the **AI Core owner** first: per-model API contract, warm/cold backend lifecycle and resource admission within the pre-existing host limits.
3. Prove two warm plus two on-demand backends in isolation, including cold-start, readiness, active-request safety, idle reclaim, actual memory recovery and rollback.
4. Make only the necessary DebugAI adapter routing changes; preserve role contracts, Thinking, prompt caching, deadlines, instrumentation and workflows.
5. Run multi-client admission tests and runtime acceptance before switching production.

This repository initially contains the safe migration/bootstrap files only. **A passed import is not a production deployment.**
