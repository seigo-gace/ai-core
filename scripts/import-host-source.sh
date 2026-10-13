#!/usr/bin/env bash
# Import only verified AI Core source files from the already-running host.
# Run from an isolated clone under /home/admin1/projects/ai-core/.worktrees/.
set -Eeuo pipefail
SOURCE_ROOT=/home/admin1/projects/ai-core
EXPECTED_BRANCH=import/server-baseline-20261013
fail() { printf 'SOURCE_IMPORT=FAIL REASON=%s\n' "$1" >&2; return 1; }
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || { fail NO_GIT_CHECKOUT; exit 1; }
source_real="$(realpath -e "$SOURCE_ROOT")" || { fail SOURCE_MISSING; exit 1; }
dest_real="$(realpath -e "$repo_root")" || { fail DEST_MISSING; exit 1; }
case "$dest_real" in "$source_real"/.worktrees/*) ;; *) fail DEST_NOT_INSIDE_EXISTING_AI_CORE; exit 1;; esac
branch="$(git -C "$repo_root" symbolic-ref --short HEAD 2>/dev/null)" || { fail DETACHED_HEAD; exit 1; }
[[ "$branch" == "$EXPECTED_BRANCH" ]] || { fail WRONG_BRANCH; exit 1; }
remote="$(git -C "$repo_root" remote get-url origin 2>/dev/null)" || { fail NO_REMOTE; exit 1; }
if [[ ! "$remote" =~ ^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)seigo-gace/ai-core(\.git)?$ ]]; then
  fail UNEXPECTED_GIT_REMOTE; exit 1
fi
[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || { fail DIRTY_CHECKOUT; exit 1; }
[[ -f "$SOURCE_ROOT/compose.runtime.yml" && -f "$SOURCE_ROOT/router/scripts/runtime_contract_gate.py" && -f "$SOURCE_ROOT/router/tests/test_router_core.py" ]] || { fail SOURCE_CONTRACT_FILES_MISSING; exit 1; }
files=(compose.runtime.yml router/Dockerfile.llama-swap router/docker-compose.llama-swap.yml router/IMPLEMENTATION_STATUS.md router/config/README.txt router/evidence/llama-swap-v257-contract.json)
[[ -f "$SOURCE_ROOT/README-ai-core.md" ]] && files+=(README-ai-core.md)
shopt -s nullglob
for file in "$SOURCE_ROOT"/router/scripts/*.py "$SOURCE_ROOT"/router/scripts/*.sh "$SOURCE_ROOT"/router/tests/*.py; do
  [[ -f "$file" ]] && files+=("${file#"$SOURCE_ROOT"/}")
done
[[ ${#files[@]} -ge 16 ]] || { fail INCOMPLETE_SOURCE_ALLOWLIST; exit 1; }
for file in "${files[@]}"; do
  [[ -f "$SOURCE_ROOT/$file" && ! -L "$SOURCE_ROOT/$file" ]] || { fail SOURCE_FILE_NOT_REGULAR; exit 1; }
  mkdir -p "$repo_root/$(dirname "$file")"
  install -m 0644 "$SOURCE_ROOT/$file" "$repo_root/$file"
done
git -C "$repo_root" add -- "${files[@]}"
git -C "$repo_root" diff --cached --check
python3 - "$repo_root" <<'PY'
from pathlib import Path
import re, subprocess, sys
repo = Path(sys.argv[1])
paths = subprocess.check_output(['git','-C',str(repo),'diff','--cached','--name-only','-z']).decode().split('\0')
paths = [p for p in paths if p]
patterns = (
    rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',
    rb'\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{18,}\b',
    rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b',
    rb'(?i)\b(?:CF_ACCESS_CLIENT_SECRET|LLAMA_SWAP_API_KEY|AI_CORE_API_KEY)\s*[:=]\s*["\x27]?[A-Za-z0-9_-]{20,}'
)
if not paths:
    raise SystemExit('IMPORT_FAILED_EMPTY_DIFF')
for path in paths:
    allowed = (path in {'compose.runtime.yml','README-ai-core.md',
        'router/Dockerfile.llama-swap','router/docker-compose.llama-swap.yml',
        'router/IMPLEMENTATION_STATUS.md','router/config/README.txt',
        'router/evidence/llama-swap-v257-contract.json'}
        or (path.startswith(('router/scripts/','router/tests/')) and path.endswith(('.py','.sh'))))
    if not allowed or any(part.startswith('.') for part in Path(path).parts):
        raise SystemExit('IMPORT_FAILED_UNEXPECTED_PATH')
    data=(repo/path).read_bytes()
    if any(re.search(p,data) for p in patterns):
        raise SystemExit('IMPORT_FAILED_SENSITIVE_CONTENT_DETECTED')
print('SOURCE_IMPORT_ALLOWLIST=PASS FILES='+str(len(paths)))
PY
(
  cd "$repo_root"
  python3 -m compileall -q router/scripts router/tests
  bash -n router/scripts/*.sh
  python3 -m unittest discover -s router/tests -p 'test_*.py'
  git diff --cached --stat
  git commit -m 'feat: import existing AI Core host source and resource guards'
  GIT_TERMINAL_PROMPT=0 git push --set-upstream origin "$EXPECTED_BRANCH"
  printf 'SOURCE_IMPORT=PASS BRANCH=%s HEAD=%s\n' "$EXPECTED_BRANCH" "$(git rev-parse HEAD)"
)
