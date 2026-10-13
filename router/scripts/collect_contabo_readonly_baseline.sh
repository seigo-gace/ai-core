#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"; STAMP="$(date -u +%Y%m%dT%H%M%SZ)"; OUT="$ROOT/evidence/readonly-$STAMP"; mkdir -p "$OUT"
for p in 18081 18082 18083 18084; do curl -fsS --max-time 5 "http://127.0.0.1:$p/health" > "$OUT/health-$p.json"; curl -fsS --max-time 5 "http://127.0.0.1:$p/v1/models" > "$OUT/models-$p.json"; done
python3 scripts/resource_baseline.py --output "$OUT/resource-baseline.json"
python3 scripts/discover_backend_compose.py > "$OUT/backend-compose-discovery.json"
echo "CONTABO_READONLY_BASELINE=PASS|$OUT"
