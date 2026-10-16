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
