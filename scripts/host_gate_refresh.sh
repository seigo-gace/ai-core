#!/usr/bin/env bash
# Reversible exact-source Gate refresh, using the existing authorized Host.
# Never creates a new API credential, service, project, or model.
set -Eeuo pipefail

if [[ $# != 2 || ! "$1" =~ ^[0-9a-f]{40}$ || ! "$2" =~ ^ai-core-model-gate:offline-[0-9a-f]{8}$ ]]; then
  echo AI_CORE_GATE_REFRESH_INVALID_SHA
  exit 64
fi
expected_head="$1"
root=/home/admin1/projects/ai-core
worktree="$root/.worktrees/pr3-verification-20261015"
new_image="$2"
old_image="ai-core-model-gate:rollback-before-${new_image##*-}"
compose="$worktree/compose.gate.yml"

[[ "$(git -C "$worktree" rev-parse HEAD)" == "$expected_head" ]]
[[ -z "$(git -C "$worktree" status --porcelain --untracked-files=all)" ]]
[[ -f "$root/.env" && -f "$compose" ]]
docker image inspect "$new_image" >/dev/null
docker inspect ai-core-model-gate >/dev/null

# No destructive back-end stop is permitted. Idle inherited residents
# remain running across this Gate refresh and are re-admitted by preflight.
for model in granite qwen3 ministral coder; do
  if [[ "$(docker inspect -f '{{.State.Running}}' "ai-$model")" == true ]]; then
    docker exec ai-core-model-gate python3 -c '
import json,sys
from runtime.model_gate import backend_http_connection
c=backend_http_connection({"backend_socket":"/run/ai-core-backend/"+sys.argv[1]+".sock"},timeout=5)
try:
    c.request("GET","/slots")
    r=c.getresponse()
    slots=json.loads(r.read(65536)) if r.status==200 else None
    assert isinstance(slots,list) and slots
    assert all(isinstance(x,dict) and x.get("is_processing") is False for x in slots)
finally:
    c.close()
' "$model"
  fi
done

# Fail closed if any other AI product is currently using the Coordinator.
docker exec ai-core-model-gate python3 -c '
import json,os,urllib.request
h={"Authorization":"Bearer "+os.environ["AI_CORE_API_KEY"]}
for path in ("/internal/handoff/status","/internal/resources/status"):
    u=urllib.request.Request("http://127.0.0.1:18184"+path,headers=h)
    with urllib.request.urlopen(u,timeout=8) as response:
        data=json.load(response)
    if path.endswith("/status") and "resident" in data:
        assert len(data["resident"]) <= 2
        assert not any(data["active"].values())
        assert not data["handoff"] and not data["recovery_pending"]
    elif "queued" in data:
        assert data["queued"] == 0
'
expected_digest="$(sha256sum "$worktree/runtime/model_gate.py" | cut -d ' ' -f1)"
image_digest="$(docker run --rm --network none --entrypoint sha256sum "$new_image" /app/runtime/model_gate.py | cut -d ' ' -f1)"
[[ "$image_digest" == "$expected_digest" ]]

# Only reuse the active Gate's already provisioned credential; never log it.
export AI_CORE_API_KEY="$(docker exec ai-core-model-gate sh -c 'printf "%s" "$AI_CORE_API_KEY"')"
[[ ${#AI_CORE_API_KEY} -ge 16 ]]
docker compose -p ai-core-gate -f "$compose" --project-directory "$root" --env-file "$root/.env" config -q
if docker image inspect "$old_image" >/dev/null 2>&1; then
  # A prior refresh can fail *after* it switched the tag and rolled back.
  # Safely reuse only the exact saved image currently active; never overwrite
  # a rollback tag which belongs to a different deployment.
  active_id="$(docker inspect -f '{{.Image}}' ai-core-model-gate)"
  saved_id="$(docker image inspect -f '{{.Id}}' "$old_image")"
  if [[ "$active_id" != "$saved_id" ]]; then
    echo AI_CORE_GATE_ROLLBACK_IMAGE_MISMATCH
    exit 75
  fi
else
  docker image tag ai-core-model-gate:local "$old_image"
fi

restore_previous() {
  code="$?"
  trap - ERR
  set +e
  docker image tag "$old_image" ai-core-model-gate:local
  docker compose -p ai-core-gate -f "$compose" --project-directory "$root" --env-file "$root/.env" up -d --no-build --force-recreate gate >/dev/null
  restore_code="$?"
  echo "AI_CORE_GATE_REFRESH=FAIL_ROLLBACK_REQUESTED ORIGINAL_EXIT=$code RESTORE_EXIT=$restore_code"
  exit "$code"
}
trap restore_previous ERR

docker image tag "$new_image" ai-core-model-gate:local
docker compose -p ai-core-gate -f "$compose" --project-directory "$root" --env-file "$root/.env" up -d --no-build --force-recreate gate >/dev/null
# Container restart and socket binding are asynchronous. Do not mark a
# successful Compose recreate as failure merely because readiness needs seconds.
# Poll only bounded liveness and exact Source hash; no model inference.
healthy=NO
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if docker exec ai-core-model-gate python3 -c '
import urllib.request
for port in (18181,18182,18183,18184):
    urllib.request.urlopen("http://127.0.0.1:%d/health"%port,timeout=2).close()
' >/dev/null 2>&1; then
    healthy=YES
    break
  fi
  sleep 2
done
[[ "$healthy" == YES ]]
actual_digest="$(docker exec ai-core-model-gate sha256sum /app/runtime/model_gate.py | cut -d ' ' -f1)"
[[ "$actual_digest" == "$expected_digest" ]]
trap - ERR
echo "AI_CORE_GATE_REFRESH=PASS SOURCE_HEAD=$expected_head SOURCE_PARITY=PASS HEALTH=4/4 OLD_IMAGE_RETAINED=YES"
