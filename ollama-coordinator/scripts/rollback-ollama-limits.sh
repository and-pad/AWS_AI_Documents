#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  exec sudo -- "$0" "$@"
fi
DROPIN_FILE=/etc/systemd/system/ollama.service.d/10-inference-limits.conf
[[ -f "$DROPIN_FILE" ]] || { echo "No coordinator inference-limit drop-in exists."; exit 0; }
read -r -p "Remove only 10-inference-limits.conf and restart Ollama? Type RESTART OLLAMA: " confirmation
[[ "$confirmation" == "RESTART OLLAMA" ]] || { echo "Cancelled."; exit 1; }
rm -f "$DROPIN_FILE"
systemctl daemon-reload
systemctl restart ollama.service
systemctl --no-pager --full status ollama.service
echo "Other Ollama systemd overrides were preserved."
