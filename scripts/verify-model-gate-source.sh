#!/usr/bin/env bash
# Source-only verification: does not start, stop, or recreate Docker containers.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 -m py_compile runtime/model_gate.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 runtime/model_gate.py --preflight
printf 'MODEL_GATE_SOURCE_VERIFY=PASS HEAD=%s\n' "$(git rev-parse HEAD)"
