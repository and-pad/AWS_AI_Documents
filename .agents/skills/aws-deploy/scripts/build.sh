#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/aws-deploy-common.sh"
cd "$REPO_ROOT"
python3 infrastructure/aws/assemble_template.py
"$SAM_CLI" validate --lint --template-file infrastructure/aws/.template-assembled.yml
"$SAM_CLI" build --template-file infrastructure/aws/.template-assembled.yml
