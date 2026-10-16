# AI Core — G-ACE shared local inference runtime

**Source owner:** this repository. **Current runtime source:** `/home/admin1/projects/ai-core` on the existing host until a verified, reversible migration is completed.

## Design authority

**AI Core's adopted design (after the owning design PR is merged to main):** [Dynamic residency, temporary three-model handoff and shared resource reuse](docs/DYNAMIC_RESIDENCY_HANDOFF_DESIGN.md). The document fixes the Master-approved 36 GiB / 6-core ceiling, normally 0–2 variable resident models, and only during bounded handoff up to 3 residents, followed by returning to <=2. The older “two initial warm models” and “always at most two” phrasing below is historical bootstrap context, not superseding authority. Implementation PR #3 must be checked against this design before production cutover.

## Goal

Make **one model = one independently addressable local API**, reusing the existing four llama.cpp backends instead of forcing every AI product through one four-model endpoint. Preserve the **host-wide AI CPU/RAM budget**, model-level resource ceilings, measured tuning, cgroup v2 protection, validation and rollback. **DebugAI's first acceptance case is four model types, six roles, but only two resident models** with the others available on demand. Do not equate independent APIs with automatic RAM release.

## Source and secret boundary

The four existing llama.cpp backends and their Docker/cgroup protection are reused by AI Core. The legacy Router importer and startup artifacts have been retired; do not restore them. Work remains on the existing AI Core PR #3 and its authorized Host checkout. Never commit `.env*`, API keys, model weights, generated host configuration, runtime evidence or secrets.

## Existing contracts to preserve

- The four existing Granite/Qwen3/Ministral/Coder llama.cpp backends are currently reachable from the Gate only through a shared **private Unix-socket Volume**. Bootstrap loopback TCP ports 18081–18084 are historical and must not be republished.
- The old llama-swap 18080 endpoint is retired and must remain stopped. It is not a compatibility dependency or rollback route. All consumers must use authenticated model-specific Gates through the shared Coordinator; direct backend requests without a lease are forbidden.
- The new shared Coordinator is the sole lifecycle and admission owner. Resource protection and validation logic formerly kept under `router/` must be retained only where needed for the new Gate/Compose path; retired Router configuration must not be launched.
- Existing `ai-core.slice`, `compose.runtime.yml`, generated resource plan, overrides, actual-value gates and rollback are source assets to reuse.
- Current host resource authority and production state are **not** changed by bootstrapping this repository.

## AI Core implementation and acceptance

1. Maintain the authenticated model-specific Gates 18181–18184 and one shared Coordinator as the sole lifecycle/lease owner.
2. Keep four selectable backends; normally at most two resident, with a bounded temporary third during handoff. Preserve approved Host cgroup limits and recover safely on failures.
3. Complete fair bounded admission/deadlines, request cancellation, streaming integrity, model-specific policies and crash recovery in the AI Core codebase.
4. Validate actual Gate inference, cold start/reclaim/handoff, memory and CPU, and source/runtime identity through the existing admitted Host route. Never restart or use 18080 as a fallback.
5. Validate the generic API with distinct consumers without implementing their product-specific roles, prompts, workflow or adapters in this repository. Keep Source, Host and consumer E2E evidence separate.

**Source or synthetic test PASS is not production deployment.**
