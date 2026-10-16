#!/usr/bin/env bash
set -Eeuo pipefail
expected="${1:?exact branch head required}"
root=/home/admin1/projects/ai-core
worktree="$root/.worktrees/pr3-verification-20261015"
[[ "$(git -C "$worktree" rev-parse HEAD)" == "$expected" ]]
[[ -z "$(git -C "$worktree" status --porcelain --untracked-files=all)" ]]
[[ "$(docker inspect -f '{{.State.Running}}' ai-core-model-gate)" == true ]]
[[ "$(docker inspect -f '{{.State.Running}}' astera-v8)" == true ]]
js="scripts/host_astera_second_consumer_probe.js"
[[ -f "$worktree/$js" ]]
container_script=/tmp/gace_ai_core_ast_v8_second_consumer.js
docker exec -i astera-v8 sh -c 'umask 077; cat > /tmp/gace_ai_core_ast_v8_second_consumer.js' < "$worktree/$js"
key="$(docker exec ai-core-model-gate sh -c 'printf "%s" "$AI_CORE_API_KEY"')"
[[ "${#key}" -ge 16 ]]
result=0
output="$(printf '%s' "$key" | docker exec -i astera-v8 node "$container_script" 2>&1)" || result=$?
unset key
docker exec astera-v8 rm -f "$container_script"
if [[ "$result" -eq 0 && "$output" == *'"authenticated":true'* ]]; then
  note="AI_CORE_ASTERA_SECOND_CONSUMER_RUNTIME source=$expected $output"
  outcome=PASS
else
  case "$output" in
    *MODULE_NOT_FOUND*) category=NODE_MODULE_NOT_FOUND ;;
    *ECONNREFUSED*) category=CONNECTION_REFUSED ;;
    *UNAUTHORIZED*|*401*) category=AUTH_DENIED ;;
    *SyntaxError*) category=NODE_SYNTAX ;;
    *HTTP*) category=HTTP_ERROR ;;
    *) category=UNCLASSIFIED ;;
  esac
  note="AI_CORE_ASTERA_SECOND_CONSUMER_FAILURE source=$expected rc=$result class=$category chars=${#output}"
  outcome=FAIL
fi
gh api -X POST repos/seigo-gace/ai-core/issues/1/comments -f "body=$note" --jq .html_url >/dev/null
printf 'SECOND_CONSUMER_RESULT=%s\n' "$outcome"
[[ "$outcome" == PASS ]]
