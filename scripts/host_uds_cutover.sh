#!/usr/bin/env bash
set -Eeuo pipefail
r=/home/admin1/projects/ai-core
w="$r/.worktrees/pr3-verification-20261015"
target="$(git -C "$w" rev-parse HEAD)"
[ "$(git -C "$w" hash-object "$w/runtime/model_gate.py")" = d7cd06b7bb6f50a544ab3fd7a8d739d245f82ebd ]
[ "$(git -C "$w" hash-object "$w/compose.runtime.yml")" = 1ba2b2d787d09b74e8956359c65809b3c032d91d ]
[ "$(git -C "$w" hash-object "$w/compose.gate.yml")" = e709c161e2a3d08ef7b02cf5482b148bb3ed511c ]
[ -z "$(git -C "$w" status --porcelain)" ]
[ -f "$r/compose.runtime.yml" ] && [ -f "$r/compose.gate.yml" ] && [ -f "$r/.env" ]
[ -d "$r/models" ]
[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' ai-coder)" = "$r/models" ]
for n in ai-coder ai-qwen3 ai-granite ai-ministral; do
  [ "$(docker inspect -f '{{.State.Running}}' "$n")" = false ]
done
[ "$(docker inspect -f '{{.State.Running}}' ai-core-model-gate)" = true ]
docker image inspect ai-core-model-gate:rollback-75778b66 ai-core-model-gate:uds-stage-75778b66 >/dev/null
key="$(docker exec ai-core-model-gate sh -c 'printf "%s" "$AI_CORE_API_KEY"')"
[ "$(printf '%s' "$key" | wc -c)" -ge 16 ]
export AI_CORE_API_KEY="$key"
unset key
docker compose -p ai-core -f "$w/compose.runtime.yml" --project-directory "$r" config -q
docker compose -p ai-core-gate -f "$w/compose.gate.yml" --project-directory "$r" --env-file "$r/.env" config -q
docker exec ai-core-model-gate python3 -c 'import os,json,urllib.request,sys; req=urllib.request.Request("http://127.0.0.1:18184/internal/handoff/status",headers={"Authorization":"Bearer "+os.environ["AI_CORE_API_KEY"]}); d=json.load(urllib.request.urlopen(req,timeout=8)); sys.exit(0 if not(d["resident"] or any(d["active"].values()) or d["handoff"] or d["recovery_pending"]) else 5)'
rollback() {
  rc=$?
  trap - ERR
  set +e
  echo "UDS_CUTOVER=FAIL_ROLLBACK_BEGIN original_exit=$rc"
  docker compose -p ai-core -f "$r/compose.runtime.yml" --project-directory "$r" create --no-build --force-recreate coder qwen3 granite ministral
  backend_rc=$?
  docker image tag ai-core-model-gate:rollback-75778b66 ai-core-model-gate:local
  image_rc=$?
  docker compose -p ai-core-gate -f "$r/compose.gate.yml" --project-directory "$r" --env-file "$r/.env" up -d --no-build --force-recreate gate
  gate_rc=$?
  echo "UDS_ROLLBACK_RESULTS backend=$backend_rc image=$image_rc gate=$gate_rc"
  exit "$rc"
}
trap rollback ERR
docker stop ai-core-model-gate >/dev/null
docker compose -p ai-core -f "$w/compose.runtime.yml" --project-directory "$r" create --no-build --force-recreate coder qwen3 granite ministral
docker image tag ai-core-model-gate:uds-stage-75778b66 ai-core-model-gate:local
docker compose -p ai-core-gate -f "$w/compose.gate.yml" --project-directory "$r" --env-file "$r/.env" up -d --no-build --force-recreate gate
for n in ai-coder ai-qwen3 ai-granite ai-ministral; do
  [ "$(docker inspect -f '{{.State.Running}}' "$n")" = false ]
  [ "$(docker inspect -f '{{.HostConfig.CgroupParent}}' "$n")" = ai-core.slice ]
  [ "$(docker inspect -f '{{.HostConfig.NanoCpus}}' "$n")" = 3000000000 ]
  [ "$(docker inspect -f '{{.HostConfig.Memory}}' "$n")" = 9663676416 ]
  ports="$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$n")"
  [ "$ports" = '{}' ] || [ "$ports" = null ]
  [ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/run/ai-core-backend"}}yes{{end}}{{end}}' "$n")" = yes ]
done
[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/run/ai-core-backend"}}yes{{end}}{{end}}' ai-core-model-gate)" = yes ]
[ "$(docker exec ai-core-model-gate sha256sum /app/runtime/model_gate.py | cut -d' ' -f1)" = "$(sha256sum "$w/runtime/model_gate.py" | cut -d' ' -f1)" ]
docker exec ai-core-model-gate python3 -c 'import urllib.request; [urllib.request.urlopen("http://127.0.0.1:%d/health"%p,timeout=5).close() for p in (18181,18182,18183,18184)]'
trap - ERR
report="AI_CORE_UDS_CUTOVER=PASS
SOURCE_HEAD=$target
GATE_SOURCE_PARITY=PASS
BACKENDS_UNPUBLISHED=4/4
SHARED_PRIVATE_BUS=5/5
MODEL_RESOURCE_LIMITS=4/4
MODELS_RESIDENT=0
OLD_ROUTER_RESTART=NO"
printf '%s\n' "$report"
if gh api -X POST repos/seigo-gace/ai-core/issues/1/comments -f "body=$report" --jq .html_url; then echo 'CUTOVER_EVIDENCE_PUBLISHED=YES'; else echo 'CUTOVER_EVIDENCE_PUBLISHED=NO'; fi
