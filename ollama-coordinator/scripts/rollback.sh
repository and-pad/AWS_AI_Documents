#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  exec sudo -- "$0" "$@"
fi
read -r -p "Disable the coordinator service (installed code/config will be retained)? Type DISABLE: " confirmation
[[ "$confirmation" == "DISABLE" ]] || { echo "Cancelled."; exit 1; }
systemctl disable --now ollama-inference-coordinator.service
rm -f /etc/systemd/system/ollama-inference-coordinator.service
systemctl daemon-reload
systemctl reset-failed ollama-inference-coordinator.service 2>/dev/null || true
echo "Coordinator stopped. Ollama settings and Apache configuration were not changed."
