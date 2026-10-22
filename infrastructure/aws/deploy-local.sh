#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

# LocalStack Lambda containers run on Docker's default bridge. The processor's
# published port is reachable through that bridge's host gateway. Resolve the
# gateway from Docker instead of baking a machine-specific address into SAM.
host_gateway="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
if [[ -z "$host_gateway" ]]; then
  echo "No se pudo determinar el gateway de la red Docker bridge" >&2
  exit 1
fi

python3 infrastructure/aws/assemble_template.py
lstk sam build --template-file infrastructure/aws/.template-assembled.yml
lstk sam deploy \
  --template-file .aws-sam/build/template.yaml \
  --config-file "$repo_root/infrastructure/aws/samconfig.toml" \
  --region us-east-1 \
  --parameter-overrides \
    Environment=dev \
    ProcessorDeployment=local \
    QueueVisibilityTimeout=900 \
    FrontendOrigin=http://localhost:5173 \
    "OllamaBaseUrl=http://${host_gateway}:11434" \
    "DocumentProcessorUrl=http://${host_gateway}:8000/process"

# LocalStack's SAM deploy left the template's ReservedConcurrentExecutions
# unset in the live Lambda configuration, so apply the same limit explicitly.
function_arn="$(lstk aws cloudformation describe-stacks \
  --stack-name document-ai-pipeline \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='DocumentProcessorFunctionArn'].OutputValue" \
  --output text)"
function_name="${function_arn##*:}"
lstk aws lambda put-function-concurrency \
  --function-name "$function_name" \
  --region us-east-1 \
  --reserved-concurrent-executions 1
