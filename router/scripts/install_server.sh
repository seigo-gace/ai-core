#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
: "${LLAMA_SWAP_API_KEY:?export LLAMA_SWAP_API_KEY first}"
DRY_RUN="${AI_CORE_DRY_RUN:-0}"; STAMP="$(date -u +%Y%m%dT%H%M%SZ)"; EVIDENCE_DIR="$ROOT/evidence/server-$STAMP"
mkdir -p "$EVIDENCE_DIR" "$ROOT/systemd" "$ROOT/generated/backend-overrides"
BACKENDS_CHANGED=0; ROUTER_STARTED=0; SLICE_INSTALLED=0; SLICE_EXISTED=0; SLICE_BACKUP=""; MANIFEST="$ROOT/generated/backend-overrides/manifest.json"
log(){ printf '%s\n' "$*"; }; fail(){ log "LLAMA_ROUTER_INSTALL=FAIL|$*"; exit 1; }
choose_docker(){
  if [[ "$DRY_RUN" == 1 ]]; then DOCKER=(docker); export AI_CORE_DOCKER_PREFIX=docker; return; fi
  sudo -v
  if docker info >/dev/null 2>&1; then DOCKER=(docker); export AI_CORE_DOCKER_PREFIX=docker
  elif sudo -n docker info >/dev/null 2>&1; then DOCKER=(sudo -n docker); export AI_CORE_DOCKER_PREFIX='sudo -n docker'
  else fail DOCKER_UNAVAILABLE; fi
}
compose(){ "${DOCKER[@]}" compose -f "$ROOT/docker-compose.llama-swap.yml" "$@"; }
wait_http(){ local url="$1" max="${2:-120}" e=0; while ((e<max)); do curl -fsS --max-time 3 "$url" >/dev/null 2>&1 && return 0; sleep 2; e=$((e+2)); done; return 1; }
backend_health(){ local p; for p in 18081 18082 18083 18084; do wait_http "http://127.0.0.1:$p/health" 120 || fail "BACKEND_HEALTH_TIMEOUT=$p"; done; log BACKEND_HEALTH=PASS; }
restore_slice(){
  [[ "$DRY_RUN" == 1 || "$SLICE_INSTALLED" != 1 ]] && return 0
  if [[ "$SLICE_EXISTED" == 1 && -f "$SLICE_BACKUP" ]]; then sudo -n install -d -m 0755 /etc/systemd/system/ai-core.slice.d; sudo -n install -m 0644 "$SLICE_BACKUP" /etc/systemd/system/ai-core.slice.d/50-resource.conf
  else sudo -n rm -f /etc/systemd/system/ai-core.slice.d/50-resource.conf; fi
  sudo -n systemctl daemon-reload || true; sudo -n systemctl try-restart ai-core.slice || true
}
rollback(){ local rc=$?; trap - ERR INT TERM; set +e; [[ "$ROUTER_STARTED" == 1 ]] && compose down >/dev/null 2>&1; [[ "$BACKENDS_CHANGED" == 1 && -f "$MANIFEST" ]] && python3 scripts/apply_backend_overrides.py --manifest "$MANIFEST" --rollback; restore_slice; exit "$rc"; }
trap rollback ERR INT TERM
choose_docker; backend_health
python3 scripts/generate_config.py --output config/config.generated.yaml
python3 scripts/preflight.py --config config/config.generated.yaml --check-live
python3 scripts/runtime_contract_gate.py --config config/config.generated.yaml
compose config --quiet
if [[ "$DRY_RUN" == 1 ]]; then
  log 'REAL_BINARY_VALIDATE=PENDING_DRY_RUN'; log 'RESOURCE_AND_BACKEND_CGROUP=PENDING_DRY_RUN'; log 'ROUTER_E2E=PENDING_DRY_RUN'; log 'LLAMA_ROUTER_INSTALL_GATE=PASS_DRY_RUN'; exit 0
fi
compose build --pull llama-swap
IMG="$("${DOCKER[@]}" image inspect ai-core-llama-swap:v257 --format '{{.Id}}' 2>/dev/null || true)"; [[ -n "$IMG" ]] || fail IMAGE_MISSING
"${DOCKER[@]}" run --rm --network host -e LLAMA_SWAP_API_KEY -v "$ROOT/config/config.generated.yaml:/etc/llama-swap/config.yaml:ro" --entrypoint /usr/local/bin/llama-swap "$IMG" -config /etc/llama-swap/config.yaml -validate | tee "$EVIDENCE_DIR/llama-swap-validate.txt"
python3 scripts/resource_baseline.py --output "$EVIDENCE_DIR/resource-baseline.json"
python3 scripts/generate_resource_policy.py --baseline "$EVIDENCE_DIR/resource-baseline.json" --slice-output "$ROOT/systemd/50-resource.conf" --plan-output "$EVIDENCE_DIR/resource-plan.json"
if sudo -n test -f /etc/systemd/system/ai-core.slice.d/50-resource.conf; then SLICE_EXISTED=1; SLICE_BACKUP="$(mktemp)"; sudo -n cat /etc/systemd/system/ai-core.slice.d/50-resource.conf > "$SLICE_BACKUP"; fi
SLICE_INSTALLED=1; bash scripts/install_resource_slice.sh | tee "$EVIDENCE_DIR/resource-slice-install.txt"
python3 scripts/discover_backend_compose.py > "$EVIDENCE_DIR/backend-compose-discovery.json"
python3 scripts/prepare_backend_overrides.py --discovery-json "$EVIDENCE_DIR/backend-compose-discovery.json" --manifest-output "$MANIFEST"
python3 scripts/apply_backend_overrides.py --manifest "$MANIFEST" --validate-only
python3 scripts/apply_backend_overrides.py --manifest "$MANIFEST" --apply; BACKENDS_CHANGED=1; backend_health
compose up -d llama-swap; ROUTER_STARTED=1; wait_http http://127.0.0.1:18080/health 120 || fail ROUTER_HEALTH_TIMEOUT
python3 scripts/e2e_llama_swap_gate.py --api-key "$LLAMA_SWAP_API_KEY" | tee "$EVIDENCE_DIR/router-e2e.txt"
python3 scripts/resource_ceiling_gate.py --plan "$EVIDENCE_DIR/resource-plan.json" | tee "$EVIDENCE_DIR/resource-ceiling-gate.txt"
trap - ERR INT TERM; log LLAMA_ROUTER_INSTALL_GATE=PASS; log "EVIDENCE_DIR=$EVIDENCE_DIR"
