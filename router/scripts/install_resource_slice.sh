#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
POLICY="$ROOT/systemd/50-resource.conf"
[[ -f "$POLICY" ]] || { echo "RESOURCE_SLICE_INSTALL=FAIL|missing_policy"; exit 1; }
sudo -n install -d -m 0755 /etc/systemd/system/ai-core.slice.d
sudo -n install -m 0644 "$POLICY" /etc/systemd/system/ai-core.slice.d/50-resource.conf
sudo -n systemctl daemon-reload
sudo -n systemctl start ai-core.slice
state="$(systemctl show ai-core.slice -p ActiveState --value)"
[[ "$state" == "active" ]] || { echo "RESOURCE_SLICE_INSTALL=FAIL|state=$state"; exit 1; }
systemctl show ai-core.slice -p ActiveState -p CPUQuotaPerSecUSec -p CPUWeight -p IOWeight -p MemoryHigh -p MemoryMax
echo "RESOURCE_SLICE_INSTALL=PASS"
