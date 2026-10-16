#!/usr/bin/env bash
# Reversible exact-source Gate refresh, using the existing authorized Host.
# Never creates a new API credential, service, project, or model.
set -Eeuo pipefail

if [[ $# != 1 || ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo AI_CORE_GATE_REFRESH_INVALID_SHA
  exit 64
fi
expected_head="$1"
root=/home/admin1/projects/ai-core
worktree="$root/.worktrees/pr3-verification-20261015"
new_image=ai-core-model-gate:offline-cd8442c3
old_image=ai-core-model-gate:rollback-before-cd8442c3
compose="$worktree/compose.gate.yml"

[[ "$(git -C "$worktree" rev-parse HEAD)" == "$expected_head" ]]
[[ -z "$(git -C "$worktree" status --porcelain --untracked-files=all)" ]]
[[ -f "$root/.env" && -f "$compose" ]]
docker image inspect "$new_image" >/dev/null
docker inspect ai-core-model-gate >/dev/null

# This is an image-source-only refresh, not a model lifecycle test.
for model in ai-granite ai-qwen3 ai-ministral ai-coder; do
  [[ "$(docker inspect -f '{{.State.Running}}' "$model")" == false ]]
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
        assert not data["resident"]
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
  echo AI_CORE_GATE_ROLLBACK_TAG_ALREADY_EXISTS
  exit 75
fi
docker image tag ai-core-model-gate:local "$old_image"

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
actual_digest="$(docker exec ai-core-model-gate sha256sum /app/runtime/model_gate.py | cut -d ' ' -f1)"
[[ "$actual_digest" == "$expected_digest" ]]
docker exec ai-core-model-gate python3 -c 'import urllib.request; [urllib.request.urlopen("http://127.0.0.1:%d/health"%p,timeout=5).close() for p in (18181,18182,18183,18184)]'
trap - ERR
echo "AI_CORE_GATE_REFRESH=PASS SOURCE_HEAD=$expected_head SOURCE_PARITY=PASS HEALTH=4/4 OLD_IMAGE_RETAINED=YES"
