#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/aws-deploy-common.sh"
cd "$REPO_ROOT"
python3 infrastructure/aws/assemble_template.py
