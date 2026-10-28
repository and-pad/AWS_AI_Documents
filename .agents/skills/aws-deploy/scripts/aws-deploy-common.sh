#!/usr/bin/env bash

# Shared defaults and read-only CloudFormation helpers for the project deploy
# scripts. Keep the profile/region/stack aligned with infrastructure/aws.

set -euo pipefail

# This project's production deployment identity is intentionally fixed. Do not
# let a caller silently point writes at another account/profile/region.
AWS_PROFILE="document-ai-ohio"
AWS_REGION="us-east-2"
AWS_DEFAULT_REGION="$AWS_REGION"
STACK_NAME="${STACK_NAME:-document-ai-pipeline}"
EXPECTED_AWS_ACCOUNT_ID="714687693031"
AWS_CLI="${AWS_CLI:-aws}"
DOCKER_CLI="${DOCKER_CLI:-docker}"
SAM_CLI="${SAM_CLI:-sam}"

export AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION STACK_NAME
export EXPECTED_AWS_ACCOUNT_ID

# This workflow deliberately uses the named profile. Do not accidentally let
# exported static credentials shadow the user's refreshed profile session.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CREDENTIAL_EXPIRATION

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

aws_profiled() {
  "$AWS_CLI" "$@" --profile "$AWS_PROFILE" --region "$AWS_REGION" --no-cli-pager
}

print_login_recovery() {
  cat >&2 <<'EOF'
AWS credentials could not be verified. Run this command yourself in your terminal:
  aws login --remote \
    --profile document-ai-ohio \
    --region us-east-2
Do not use aws sso login or static credentials.
EOF
}

read_caller_identity() {
  local identity_json
  identity_json="$("$AWS_CLI" sts get-caller-identity \
    --profile document-ai-ohio --region us-east-2 --output json --no-cli-pager 2>/dev/null)" || return 1
  CALLER_ACCOUNT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("Account", ""))' "$identity_json")" || return 1
  CALLER_ARN="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("Arn", ""))' "$identity_json")" || return 1
  [[ -n "$CALLER_ACCOUNT" ]]
}

ensure_no_static_profile_keys() {
  local key_name
  for key_name in aws_access_key_id aws_secret_access_key; do
    if "$AWS_CLI" configure get "$key_name" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
      echo "Static access keys are configured for profile $AWS_PROFILE; refusing deployment. Use aws login --remote instead." >&2
      return 1
    fi
  done
}

wait_for_interactive_login() {
  print_login_recovery
  if [[ ! -t 0 ]]; then
    echo "Refresh the profile, then rerun this command; no further write operation was attempted." >&2
    return 1
  fi
  printf 'After you finish aws login --remote in your terminal, press Enter here to recheck identity: ' >&2
  read -r _
}

ensure_aws_identity() {
  ensure_no_static_profile_keys || return 1
  if ! read_caller_identity; then
    wait_for_interactive_login || return 1
    ensure_no_static_profile_keys || return 1
    if ! read_caller_identity; then
      echo "AWS identity is still unavailable; stopping before any write operation." >&2
      return 1
    fi
  fi
  if [[ "$CALLER_ACCOUNT" != "$EXPECTED_AWS_ACCOUNT_ID" ]]; then
    printf 'Wrong AWS account: expected %s, received %s. Stopping before any write operation.\n' \
      "$EXPECTED_AWS_ACCOUNT_ID" "$CALLER_ACCOUNT" >&2
    return 1
  fi
  printf 'AWS identity verified: account=%s profile=%s region=%s\n' \
    "$CALLER_ACCOUNT" "$AWS_PROFILE" "$AWS_REGION"
}

stack_output() {
  local output_key="$1"
  aws_profiled cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='${output_key}'].OutputValue | [0]" \
    --output text
}

require_nonempty_value() {
  local label="$1" value="$2"
  if [[ -z "$value" || "$value" == "None" || "$value" == "null" ]]; then
    printf 'Could not resolve %s from CloudFormation stack %s.\n' "$label" "$STACK_NAME" >&2
    return 1
  fi
}

confirm_word() {
  local expected="$1" prompt="$2" answer=""
  if [[ ! -t 0 ]]; then
    printf 'Interactive confirmation is required; refusing to continue without a terminal.\n%s\n' "$prompt" >&2
    return 1
  fi
  printf '%s\nType %s to continue: ' "$prompt" "$expected"
  read -r answer
  if [[ "$answer" != "$expected" ]]; then
    printf 'Confirmation did not match; stopping without continuing.\n' >&2
    return 1
  fi
}

