#!/usr/bin/env bash

# Build the SPA with the production API/Cognito settings, then publish it to
# its private S3 origin and invalidate the configured CloudFront distribution.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=aws-deploy-common.sh
source "$SCRIPT_DIR/aws-deploy-common.sh"

readonly FRONTEND_BUCKET="document-ai-frontend-714687693031-us-east-2-an"
readonly CLOUDFRONT_DISTRIBUTION_ID="E2W9M0G33GE6HK"
readonly FRONTEND_VITE_API_BASE_URL="https://prvyiqstbc.execute-api.us-east-2.amazonaws.com/dev"
readonly FRONTEND_VITE_COGNITO_USER_POOL_ID="us-east-2_wwDVGjsAN"
readonly FRONTEND_VITE_COGNITO_USER_POOL_CLIENT_ID="4vie26va19mb50d227c6j9amqo"

cd "$REPO_ROOT"
ensure_aws_identity

echo "Building frontend with the configured production API and Cognito values."
(
  cd "$REPO_ROOT/frontend"
  VITE_API_BASE_URL="$FRONTEND_VITE_API_BASE_URL" \
    VITE_COGNITO_USER_POOL_ID="$FRONTEND_VITE_COGNITO_USER_POOL_ID" \
    VITE_COGNITO_USER_POOL_CLIENT_ID="$FRONTEND_VITE_COGNITO_USER_POOL_CLIENT_ID" \
    npm run build
)

DIST_DIR="$REPO_ROOT/frontend/dist"
if [[ ! -f "$DIST_DIR/index.html" ]]; then
  echo "Frontend build did not produce frontend/dist/index.html; nothing was published." >&2
  exit 1
fi

confirm_word "PUBLISH" \
  "This will sync frontend/dist/ to s3://$FRONTEND_BUCKET/ with --delete, then invalidate CloudFront distribution $CLOUDFRONT_DISTRIBUTION_ID."

# Recheck immediately before each AWS write. A credential refresh is user-run;
# the shared helper waits for confirmation and revalidates the expected account.
ensure_aws_identity
if ! aws_profiled s3 sync "$DIST_DIR/" "s3://$FRONTEND_BUCKET/" --delete; then
  cat >&2 <<EOF
S3 sync failed and may have partially updated the frontend bucket. It will not
be retried automatically, and no CloudFront invalidation was attempted.
Inspect the bucket before deciding whether to run this deployment again.
EOF
  exit 1
fi

ensure_aws_identity
if ! aws_profiled cloudfront create-invalidation \
    --distribution-id "$CLOUDFRONT_DISTRIBUTION_ID" --paths '/*'; then
  echo "CloudFront invalidation failed and will not be retried automatically; checking recent invalidations read-only." >&2
  if ensure_aws_identity; then
    aws_profiled cloudfront list-invalidations \
      --distribution-id "$CLOUDFRONT_DISTRIBUTION_ID" \
      --max-items 10 \
      --query 'InvalidationList.Items[].{id:Id,status:Status,created:CreateTime}' \
      --output table || true
  fi
  exit 1
fi

echo "Frontend publication and CloudFront invalidation request completed."
