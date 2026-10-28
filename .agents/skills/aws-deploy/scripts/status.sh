#!/usr/bin/env bash

# Read-only CloudFormation/ECS status helper.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/aws-deploy-common.sh"
ensure_aws_identity || exit 1

echo "CloudFormation stack: $STACK_NAME"
aws_profiled cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].{status:StackStatus,updated:LastUpdatedTime,outputs:Outputs}' --output table

CLUSTER_NAME="$(stack_output DocumentProcessorClusterName)"
SERVICE_NAME="$(stack_output DocumentProcessorServiceName)"
QUEUE_URL="$(stack_output DocumentQueueUrl)"
require_nonempty_value DocumentProcessorClusterName "$CLUSTER_NAME"
require_nonempty_value DocumentProcessorServiceName "$SERVICE_NAME"
require_nonempty_value DocumentQueueUrl "$QUEUE_URL"

echo "ECS service: $CLUSTER_NAME / $SERVICE_NAME"
aws_profiled ecs describe-services --cluster "$CLUSTER_NAME" --services "$SERVICE_NAME" \
  --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount,taskDefinition:taskDefinition,deployments:deployments,events:events[0:5]}' \
  --output json
echo "Document queue attributes (approximate SQS counters):"
aws_profiled sqs get-queue-attributes --queue-url "$QUEUE_URL" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible VisibilityTimeout \
  --output json
