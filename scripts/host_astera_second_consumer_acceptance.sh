#!/usr/bin/env bash
set -Eeuo pipefail
expected="${1:?exact branch head required}"
mode="${2:-catalogue}"
case "$mode" in catalogue|inference|legacy-inference|result) :;; *) exit 64;; esac
root=/home/admin1/projects/ai-core
worktree="$root/.worktrees/pr3-verification-20261015"
[[ "$(git -C "$worktree" rev-parse HEAD)" == "$expected" ]]
[[ -z "$(git -C "$worktree" status --porcelain --untracked-files=all)" ]]
[[ "$(docker inspect -f '{{.State.Running}}' ai-core-model-gate)" == true ]]
[[ "$(docker inspect -f '{{.State.Running}}' astera-v8)" == true ]]
js="scripts/host_astera_second_consumer_probe.js"
[[ -f "$worktree/$js" ]]
container_script=/tmp/gace_ai_core_ast_v8_second_consumer.js

if [[ "$mode" == result ]]; then
  base="/tmp/gace_ai_core_ast_v8_${expected:0:8}"
  state=$(docker exec astera-v8 cat "${base}.state")
  if [[ "$state" != DONE ]]; then
    echo "SECOND_CONSUMER_INFERENCE_STATE=$state"
    exit 0
  fi
  rc=$(docker exec astera-v8 cat "${base}.exit")
  output=$(docker exec astera-v8 tail -n 1 "${base}.result")
  if [[ "$rc" == 0 && "${#output}" -le 2000 && "$output" == *'"inference":"PASS"'* ]]; then
    outcome=PASS
    note="AI_CORE_SECOND_PRODUCT_ACTUAL_INFERENCE SOURCE=$expected $output"
  else
    outcome=FAIL
    note="AI_CORE_SECOND_PRODUCT_ACTUAL_INFERENCE SOURCE=$expected RESULT=FAIL EXIT=$rc OUTPUT_CHARS=${#output}"
  fi
  gh api -X POST repos/seigo-gace/ai-core/issues/1/comments -f "body=$note" --jq .html_url >/dev/null
  docker exec astera-v8 rm -f "$container_script" "${base}.state" "${base}.exit" "${base}.result" "${base}.pipe"
  echo "SECOND_CONSUMER_INFERENCE=$outcome"
  [[ "$outcome" == PASS ]]
  exit
fi

docker exec -i astera-v8 sh -c 'umask 077; cat > /tmp/gace_ai_core_ast_v8_second_consumer.js' < "$worktree/$js"
docker exec astera-v8 node --check "$container_script"
key="$(docker exec ai-core-model-gate sh -c 'printf "%s" "$AI_CORE_API_KEY"')"
[[ "${#key}" -ge 16 ]]

if [[ "$mode" == inference || "$mode" == legacy-inference ]]; then
  docker exec ai-core-model-gate python3 -c '
import json,os,urllib.request
h={"Authorization":"Bearer "+os.environ["AI_CORE_API_KEY"]}
def read(path):
 r=urllib.request.Request("http://127.0.0.1:18184"+path,headers=h)
 return json.load(urllib.request.urlopen(r,timeout=9))
a=read("/internal/handoff/status")
b=read("/internal/resources/status")
assert not a["handoff"] and not a["recovery_pending"]
assert not any(a["active"].values()) and b["queued"]==0
'
  base="/tmp/gace_ai_core_ast_v8_${expected:0:8}"
  docker exec astera-v8 sh -c 'test ! -e "$1" && test ! -e "$2" && test ! -e "$3"' _ "${base}.state" "${base}.pipe" "${base}.result"
  docker exec astera-v8 sh -c 'umask 077; mkfifo "$1"; echo PREPARED > "$2"' _ "${base}.pipe" "${base}.state"
  selector=""
  if [[ "$mode" == legacy-inference ]]; then selector=legacy; fi
  docker exec -d -e ASTERA_LLM_TIMEOUT_MS=240000 astera-v8 sh -c '
    node "$1" inference "$6" < "$2" > "$3" 2>&1
    rc=$?
    echo "$rc" > "$4"
    rm -f "$2"
    echo DONE > "$5"
  ' _ "$container_script" "${base}.pipe" "${base}.result" "${base}.exit" "${base}.state" "$selector"
  printf '%s' "$key" | docker exec -i astera-v8 sh -c 'cat > "$1"' _ "${base}.pipe"
  unset key
  echo SECOND_CONSUMER_INFERENCE_STARTED=YES
  exit 0
fi

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
