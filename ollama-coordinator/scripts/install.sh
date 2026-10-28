#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COORDINATOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR=/opt/ollama-inference-coordinator
CONFIG_FILE=/etc/ollama-inference-coordinator.env
UNIT_FILE=/etc/systemd/system/ollama-inference-coordinator.service

if [[ ${EUID} -ne 0 ]]; then
  exec sudo -- "$0" "$@"
fi
id ollama >/dev/null 2>&1 || { echo "The ollama system user does not exist." >&2; exit 1; }

install -d -o ollama -g ollama -m 0750 "$INSTALL_DIR/app"
install -o ollama -g ollama -m 0640 "$COORDINATOR_DIR/app/__init__.py" "$INSTALL_DIR/app/__init__.py"
install -o ollama -g ollama -m 0640 "$COORDINATOR_DIR/app/coordinator.py" "$INSTALL_DIR/app/coordinator.py"
install -o ollama -g ollama -m 0640 "$COORDINATOR_DIR/app/main.py" "$INSTALL_DIR/app/main.py"
install -o ollama -g ollama -m 0640 "$COORDINATOR_DIR/app/serve.py" "$INSTALL_DIR/app/serve.py"
install -o root -g ollama -m 0640 "$COORDINATOR_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"
chown -R ollama:ollama "$INSTALL_DIR/app" "$INSTALL_DIR/.venv"
chmod -R go-rwx "$INSTALL_DIR/.venv"

if [[ ! -e "$CONFIG_FILE" ]]; then
  install -o root -g ollama -m 0640 "$COORDINATOR_DIR/.env.example" "$CONFIG_FILE"
  docker_gateway="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
  if [[ "$docker_gateway" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    sed -i "s|^OLLAMA_COORDINATOR_DOCKER_BIND_HOST=.*$|OLLAMA_COORDINATOR_DOCKER_BIND_HOST=$docker_gateway|" "$CONFIG_FILE"
    echo "Coordinator will also bind to Docker bridge gateway $docker_gateway for local containers."
  fi
  echo "Created $CONFIG_FILE from the example; review its values before starting the service." >&2
fi
install -o root -g root -m 0644 "$COORDINATOR_DIR/systemd/ollama-inference-coordinator.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable --now ollama-inference-coordinator.service
systemctl --no-pager --full status ollama-inference-coordinator.service
echo "Coordinator listens only on 127.0.0.1:11435. Ollama was not restarted or reconfigured." 
