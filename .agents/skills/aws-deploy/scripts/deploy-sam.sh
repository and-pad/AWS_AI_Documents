#!/usr/bin/env bash

# Assemble, validate, build, and deploy the existing CloudFormation stack.
# Current deployed parameter values are carried forward; supplied overrides
# replace only matching keys (for processor deploys, DocumentProcessorImageTag).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=aws-deploy-common.sh
source "$SCRIPT_DIR/aws-deploy-common.sh"

IMAGE_TAG=""
declare -a EXTRA_OVERRIDES=()
while (($#)); do
  case "$1" in
    --image-tag)
      (($# >= 2)) || { echo "--image-tag requires a value" >&2; exit 2; }
      IMAGE_TAG="$2"
      shift 2
      ;;
    --parameter)
      (($# >= 2)) || { echo "--parameter requires KEY=VALUE" >&2; exit 2; }
      EXTRA_OVERRIDES+=("$2")
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Usage: deploy-sam.sh [--image-tag TAG] [--parameter KEY=VALUE ...]

Deploys the already-built SAM template. It does not build or publish a Docker
image. For document-processor changes use deploy-processor.sh, which publishes
the image before calling this script.
EOF
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -n "$IMAGE_TAG" ]]; then
  [[ "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || {
    echo "Invalid DocumentProcessorImageTag." >&2
    exit 2
  }
  EXTRA_OVERRIDES+=("DocumentProcessorImageTag=$IMAGE_TAG")
fi

[[ -d "$REPO_ROOT/infrastructure/aws" ]] || {
  echo "Run this script from within the AWS_AI_Documents checkout." >&2
  exit 2
}
cd "$REPO_ROOT"
ensure_aws_identity

ASSEMBLED_TEMPLATE="$REPO_ROOT/infrastructure/aws/.template-assembled.yml"
BUILD_TEMPLATE="$REPO_ROOT/.aws-sam/build/template.yaml"
SAM_CONFIG="$REPO_ROOT/infrastructure/aws/samconfig.toml"

python3 infrastructure/aws/assemble_template.py
"$SAM_CLI" validate --lint --template-file "$ASSEMBLED_TEMPLATE"
"$SAM_CLI" build --template-file "$ASSEMBLED_TEMPLATE"

# Retain the deployed values rather than allowing SAM's short samconfig.toml
# override list to reset parameters to template defaults. Filter out keys that
# are not in the newly assembled template, and fail on masked NoEcho values.
STACK_PARAMETERS="$(aws_profiled cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Parameters' \
  --output json)"
PARAMETER_OVERRIDES_TEXT="$(STACK_PARAMETERS_JSON="$STACK_PARAMETERS" python3 - "$ASSEMBLED_TEMPLATE" "$IMAGE_TAG" "${EXTRA_OVERRIDES[@]}" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

template_path = Path(sys.argv[1])
image_tag = sys.argv[2]
extra_overrides = sys.argv[3:]
template_text = template_path.read_text()
match = re.search(r"(?ms)^Parameters:\s*\n(.*?)(?=^[A-Za-z][A-Za-z0-9]*:\s*$|\Z)", template_text)
if not match:
    raise SystemExit("Could not find top-level Parameters in assembled template")
declared = set(re.findall(r"(?m)^  ([A-Za-z][A-Za-z0-9]*):\s*$", match.group(1)))
current = json.loads(os.environ.get("STACK_PARAMETERS_JSON", "[]")) or []
values = {}
for item in current:
    key = item.get("ParameterKey")
    value = item.get("ParameterValue")
    if key in declared and value is not None:
        if value == "****":
            raise SystemExit(f"CloudFormation masks parameter {key}; refusing to replace it implicitly")
        if "\0" in value:
            raise SystemExit(f"Parameter {key} cannot be represented safely")
        values[key] = value
if image_tag:
    values["DocumentProcessorImageTag"] = image_tag
for override in extra_overrides:
    key, separator, value = override.partition("=")
    if not separator or not key:
        raise SystemExit("--parameter values must be KEY=VALUE")
    if key not in declared:
        raise SystemExit(f"Unknown CloudFormation parameter: {key}")
    if "\0" in value:
        raise SystemExit(f"Parameter {key} cannot be represented safely")
    values[key] = value
if "DocumentProcessorImageTag" not in declared and "DocumentProcessorImageTag" in values:
    raise SystemExit("Assembled template does not declare DocumentProcessorImageTag")
for key, value in values.items():
    # A newline cannot be passed unambiguously through SAM's KEY=VALUE syntax.
    if "\n" in value or "\r" in value:
        raise SystemExit(f"Parameter {key} contains a newline; refusing an unsafe override")
    print(f"{key}={value}")
PY
)"
declare -a PARAMETER_OVERRIDES=()
if [[ -n "$PARAMETER_OVERRIDES_TEXT" ]]; then
  mapfile -t PARAMETER_OVERRIDES <<< "$PARAMETER_OVERRIDES_TEXT"
fi

if [[ -z "$IMAGE_TAG" && ${#EXTRA_OVERRIDES[@]} -eq 0 ]]; then
  echo "No explicit override provided; preserving the live stack parameters." >&2
fi

confirm_word "SAM" \
  "SAM is about to create a CloudFormation changeset for $STACK_NAME. Review it carefully; apply only the expected resources."

# Recheck immediately before SAM creates the CloudFormation changeset. SAM's
# built-in confirmation remains enabled for the separate apply decision.
ensure_aws_identity

set +e
"$SAM_CLI" deploy \
  --template-file "$BUILD_TEMPLATE" \
  --config-file "$SAM_CONFIG" \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE" \
  --parameter-overrides "${PARAMETER_OVERRIDES[@]}" \
  --confirm-changeset
SAM_STATUS=$?
set -e

if ((SAM_STATUS != 0)); then
  echo "sam deploy failed. It will not be retried automatically; no ECS service update was attempted outside CloudFormation." >&2
  if ensure_aws_identity; then
    echo "Checking actual CloudFormation status before any operator decides whether to rerun."
    if ! aws_profiled cloudformation describe-stacks --stack-name "$STACK_NAME" \
        --query 'Stacks[0].{status:StackStatus,updated:LastUpdatedTime}' --output table; then
      echo "Stack status read failed; revalidating the session before retrying this read-only inspection." >&2
      if ensure_aws_identity; then
        aws_profiled cloudformation describe-stacks --stack-name "$STACK_NAME" \
          --query 'Stacks[0].{status:StackStatus,updated:LastUpdatedTime}' --output table || true
      fi
    fi
    if ! aws_profiled cloudformation describe-stack-events --stack-name "$STACK_NAME" \
        --query 'StackEvents[0:15].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]' \
        --output table; then
      echo "Stack event read failed; revalidating the session before retrying this read-only inspection." >&2
      if ensure_aws_identity; then
        aws_profiled cloudformation describe-stack-events --stack-name "$STACK_NAME" \
          --query 'StackEvents[0:15].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]' \
          --output table || true
      fi
    fi
  else
    echo "Could not revalidate AWS identity to inspect CloudFormation. After authenticating, inspect stack status before rerunning any write operation." >&2
  fi
  exit "$SAM_STATUS"
fi

ensure_aws_identity
if ! STACK_STATUS="$(aws_profiled cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --query 'Stacks[0].StackStatus' --output text)"; then
  echo "CloudFormation status read failed; revalidating identity before retrying this read-only inspection." >&2
  ensure_aws_identity
  STACK_STATUS="$(aws_profiled cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --query 'Stacks[0].StackStatus' --output text)"
fi
printf 'CloudFormation stack status: %s\n' "$STACK_STATUS"
if [[ "$STACK_STATUS" != "UPDATE_COMPLETE" && "$STACK_STATUS" != "CREATE_COMPLETE" ]]; then
  echo "CloudFormation did not finish in a successful complete state." >&2
  exit 1
fi
