#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COORDINATOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DROPIN_DIR=/etc/systemd/system/ollama.service.d
DROPIN_FILE="$DROPIN_DIR/10-inference-limits.conf"

if [[ ${EUID} -ne 0 ]]; then
  exec sudo -- "$0" "$@"
fi
read -r -p "This changes Ollama model scheduling and restarts Ollama. Type RESTART OLLAMA: " confirmation
[[ "$confirmation" == "RESTART OLLAMA" ]] || { echo "Cancelled."; exit 1; }
install -d -o root -g root -m 0755 "$DROPIN_DIR"
install -o root -g root -m 0644 "$COORDINATOR_DIR/systemd/ollama-single-model.conf.example" "$DROPIN_FILE"
systemctl daemon-reload
systemctl restart ollama.service
systemctl --no-pager --full status ollama.service
echo "Verify with: systemctl show ollama -p Environment; ollama ps; nvidia-smi"
