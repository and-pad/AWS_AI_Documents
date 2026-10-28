#!/usr/bin/env bash

# Build and publish document-processor, then optionally roll it out through
# SAM/CloudFormation. ECS is never modified directly by this script.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=aws-deploy-common.sh
source "$SCRIPT_DIR/aws-deploy-common.sh"

usage() {
  cat <<'EOF'
Usage:
  deploy-processor.sh --build-push
  deploy-processor.sh --deploy [--parameter KEY=VALUE ...]
  deploy-processor.sh --deploy-published TAG [--parameter KEY=VALUE ...]

--build-push  Build linux/amd64 and publish a uniquely tagged image to ECR.
              Does not change the CloudFormation stack or ECS service.
--deploy      Preflight the running worker, build and publish one image, deploy
              SAM with that exact tag, then verify ECS stability and health.
--deploy-published TAG
              Preflight, verify that TAG exists in the stack ECR repository,
              deploy it through SAM, then verify ECS without rebuilding/pushing.
--parameter   Override a SAM parameter for this deployment. By default this
              script sets OllamaAskModel=qwen3-vl:8b-instruct for the question worker.

For expired AWS credentials, run:
  aws login --remote --profile document-ai-ohio --region us-east-2
EOF
}

MODE=""
PUBLISHED_IMAGE_TAG=""
declare -a SAM_PARAMETERS=()
while (($#)); do
  case "$1" in
    --build-push|--deploy)
      [[ -z "$MODE" ]] || { echo "Choose exactly one deployment mode." >&2; exit 2; }
      MODE="$1"
      shift
      ;;
    --deploy-published)
      [[ -z "$MODE" ]] || { echo "Choose exactly one deployment mode." >&2; exit 2; }
      (($# >= 2)) || { echo "--deploy-published requires an existing image tag." >&2; exit 2; }
      MODE="$1"
      PUBLISHED_IMAGE_TAG="$2"
      [[ "$PUBLISHED_IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || {
        echo "Invalid published image tag." >&2
        exit 2
      }
      shift 2
      ;;
    --parameter)
      (($# >= 2)) || { echo "--parameter requires KEY=VALUE" >&2; exit 2; }
      [[ "$2" == *=* && "${2%%=*}" =~ ^[A-Za-z][A-Za-z0-9]*$ && "$2" != *$'\n'* && "$2" != *$'\r'* ]] || {
        echo "--parameter must be a single-line KEY=VALUE." >&2
        exit 2
      }
      SAM_PARAMETERS+=("$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n "$MODE" ]] || { usage >&2; exit 2; }
if [[ "$MODE" == "--build-push" && ${#SAM_PARAMETERS[@]} -gt 0 ]]; then
  echo "--parameter is only valid with --deploy." >&2
  exit 2
fi
if [[ "$MODE" == "--deploy" || "$MODE" == "--deploy-published" ]]; then
  has_ask_model_override=0
  for parameter in "${SAM_PARAMETERS[@]}"; do
    [[ "$parameter" == OllamaAskModel=* ]] && has_ask_model_override=1
  done
  if ((has_ask_model_override == 0)); then
    SAM_PARAMETERS+=("OllamaAskModel=qwen3-vl:8b-instruct")
  fi
fi

cd "$REPO_ROOT"
ensure_aws_identity || exit 1

STACK_STATUS="$(aws_profiled cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].StackStatus' --output text)"
case "$STACK_STATUS" in
  CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE) ;;
  *) printf 'CloudFormation stack is not ready for deployment: %s\n' "$STACK_STATUS" >&2; exit 1 ;;
esac

ECR_URI="$(stack_output DocumentProcessorRepositoryUri)"
require_nonempty_value DocumentProcessorRepositoryUri "$ECR_URI"
CLUSTER_NAME="$(stack_output DocumentProcessorClusterName)"
require_nonempty_value DocumentProcessorClusterName "$CLUSTER_NAME"
SERVICE_NAME="$(stack_output DocumentProcessorServiceName)"
require_nonempty_value DocumentProcessorServiceName "$SERVICE_NAME"
QUEUE_URL="$(stack_output DocumentQueueUrl)"
require_nonempty_value DocumentQueueUrl "$QUEUE_URL"
JOBS_TABLE="$(stack_output JobsTableName)"
require_nonempty_value JobsTableName "$JOBS_TABLE"
RESULTS_BUCKET="$(stack_output ResultsBucketName)"
require_nonempty_value ResultsBucketName "$RESULTS_BUCKET"

if [[ "$MODE" == "--deploy-published" ]]; then
  IMAGE_TAG="$PUBLISHED_IMAGE_TAG"
else
  IMAGE_TAG="processor-$(date -u +%Y%m%dT%H%M%SZ)"
fi
IMAGE_URI="$ECR_URI:$IMAGE_TAG"
LOG_GROUP="${PROCESSOR_LOG_GROUP:-/ecs/document-ai-pipeline-dev-document-processor}"

read_preflight() {
  local service_json queue_json jobs_json task_definition service_state queue_state
  service_json="$(aws_profiled ecs describe-services \
    --cluster "$CLUSTER_NAME" --services "$SERVICE_NAME" --output json)"
  queue_json="$(aws_profiled sqs get-queue-attributes --queue-url "$QUEUE_URL" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible VisibilityTimeout \
    --output json)"
  jobs_json="$(aws_profiled dynamodb scan --table-name "$JOBS_TABLE" \
    --filter-expression '#status = :processing' \
    --expression-attribute-names '{"#status":"status"}' \
    --expression-attribute-values '{":processing":{"S":"PROCESSING"}}' \
    --projection-expression 'job_id' --output json)"

  service_state="$(python3 -c '
import json,sys
data=json.loads(sys.argv[1]); services=data.get("services", [])
if len(services) != 1: raise SystemExit("ECS service lookup did not return exactly one service")
s=services[0]
if s.get("failures"): raise SystemExit("ECS service lookup returned failures")
deployments=s.get("deployments", [])
primary=[d for d in deployments if d.get("status")=="PRIMARY"]
if len(primary)!=1: raise SystemExit("ECS service has no unique primary deployment")
d=primary[0]
print("\t".join(str(x) for x in (s.get("desiredCount",0), s.get("runningCount",0), s.get("pendingCount",0), s.get("taskDefinition",""), len(deployments), d.get("rolloutState","UNKNOWN"))))
' "$service_json")"
  IFS=$'\t' read -r DESIRED_COUNT RUNNING_COUNT PENDING_COUNT CURRENT_TASK_DEFINITION DEPLOYMENT_COUNT ROLLOUT_STATE <<< "$service_state"

  queue_state="$(python3 -c '
import json,sys
a=json.loads(sys.argv[1]).get("Attributes", {})
print("\t".join(str(a.get(k,"0")) for k in ("ApproximateNumberOfMessages","ApproximateNumberOfMessagesNotVisible","VisibilityTimeout")))
' "$queue_json")"
  IFS=$'\t' read -r QUEUE_VISIBLE QUEUE_IN_FLIGHT VISIBILITY_TIMEOUT <<< "$queue_state"

  mapfile -t ACTIVE_JOB_IDS < <(python3 -c '
import json,sys
for item in json.loads(sys.argv[1]).get("Items", []):
    value=item.get("job_id", {}).get("S")
    if value: print(value)
' "$jobs_json")
  ACTIVE_JOB_COUNT="${#ACTIVE_JOB_IDS[@]}"

  task_definition="$(aws_profiled ecs describe-task-definition \
    --task-definition "$CURRENT_TASK_DEFINITION" --output json)"
  TASK_DETAILS="$(python3 -c '
import json,sys
d=json.loads(sys.argv[1]).get("taskDefinition", {})
containers=[c for c in d.get("containerDefinitions", []) if c.get("name")=="document-processor"]
if len(containers)!=1: raise SystemExit("Could not identify document-processor container")
c=containers[0]
print("\t".join((c.get("image",""), str(c.get("stopTimeout","default")), "yes" if c.get("healthCheck") else "no")))
' "$task_definition")"
  IFS=$'\t' read -r CURRENT_IMAGE STOP_TIMEOUT HAS_HEALTHCHECK <<< "$TASK_DETAILS"

  printf 'Pre-deploy state: ECS desired=%s running=%s pending=%s deployments=%s rollout=%s\n' \
    "$DESIRED_COUNT" "$RUNNING_COUNT" "$PENDING_COUNT" "$DEPLOYMENT_COUNT" "$ROLLOUT_STATE"
  printf 'Queue: visible=%s in_flight=%s visibility_timeout_seconds=%s\n' \
    "$QUEUE_VISIBLE" "$QUEUE_IN_FLIGHT" "$VISIBILITY_TIMEOUT"
  printf 'Current task definition: %s\nCurrent image: %s\nContainer stopTimeout=%s healthCheck=%s\n' \
    "$CURRENT_TASK_DEFINITION" "$CURRENT_IMAGE" "$STOP_TIMEOUT" "$HAS_HEALTHCHECK"
  printf 'Jobs currently PROCESSING: %s\n' "$ACTIVE_JOB_COUNT"

  if [[ "$PENDING_COUNT" != "0" || "$RUNNING_COUNT" != "$DESIRED_COUNT" || "$ROLLOUT_STATE" != "COMPLETED" ]]; then
    echo "ECS service is not stable; refusing to start a processor deployment." >&2
    aws_profiled ecs describe-services --cluster "$CLUSTER_NAME" --services "$SERVICE_NAME" \
      --query 'services[0].events[0:10].[createdAt,message]' --output table || true
    return 1
  fi

  if [[ "$RUNNING_COUNT" != "0" ]]; then
    local running_tasks running_task_arns
    running_task_arns="$(aws_profiled ecs list-tasks --cluster "$CLUSTER_NAME" \
      --service-name "$SERVICE_NAME" --desired-status RUNNING --query 'taskArns' --output text)"
    if [[ -z "$running_task_arns" || "$running_task_arns" == "None" ]]; then
      echo "ECS reports a running task count but returned no task ARN." >&2
      return 1
    fi
    read -r -a running_tasks <<< "$running_task_arns"
    running_task_json="$(aws_profiled ecs describe-tasks --cluster "$CLUSTER_NAME" \
      --tasks "${running_tasks[@]}" --output json)"
    python3 -c '
import json,sys
tasks=json.loads(sys.argv[1]).get("tasks", [])
if not tasks or any(t.get("lastStatus")!="RUNNING" for t in tasks):
    raise SystemExit("Pre-deploy ECS tasks are not all RUNNING")
for task in tasks:
    for container in task.get("containers", []):
        if container.get("name")=="document-processor" and container.get("healthStatus")!="HEALTHY":
            raise SystemExit("Pre-deploy document-processor container is not HEALTHY")
print("Existing processor task passed ECS health check.")
' "$running_task_json"
  fi

  for job_id in "${ACTIVE_JOB_IDS[@]}"; do
    local checkpoint_count
    checkpoint_count="$(aws_profiled s3api list-objects-v2 --bucket "$RESULTS_BUCKET" \
      --prefix "checkpoints/$job_id/" --query 'KeyCount' --output text)"
    printf 'PROCESSING job %s has %s checkpoint object(s) in ResultsBucket.\n' "$job_id" "$checkpoint_count"
  done
}

authorize_active_job_interruption_if_needed() {
  if ((ACTIVE_JOB_COUNT > 0)) || ((QUEUE_IN_FLIGHT > 0)); then
    cat <<EOF
Warning: the preflight found $ACTIVE_JOB_COUNT PROCESSING job(s) and $QUEUE_IN_FLIGHT
in-flight DocumentQueue message(s). ECS uses a bounded SIGTERM drain (45 seconds)
and container stopTimeout=${STOP_TIMEOUT}s; a long Ollama call can be interrupted.
The SQS message and confirmed checkpoints remain available for retry, but the
current page may need to run again.
EOF
    confirm_word "INTERRUPT" "Type INTERRUPT only if you explicitly authorize updating ECS while work may be active."
  fi
}

publish_image() {
  ecr_login
  printf 'Building %s from ./document-processor for linux/amd64.\n' "$IMAGE_URI"
  "$DOCKER_CLI" build --platform linux/amd64 -t "$IMAGE_URI" ./document-processor
  # ECR push is the first write. Recheck the account after a potentially long
  # local build, then refresh Docker's short-lived ECR auth token.
  ensure_aws_identity || return 1
  ecr_login
  printf 'Publishing %s.\n' "$IMAGE_URI"
  if ! "$DOCKER_CLI" push "$IMAGE_URI"; then
    cat >&2 <<EOF
Docker push failed. It will not be retried automatically and SAM has not been
invoked. Check whether ECR received tag $IMAGE_TAG before manually rerunning.
EOF
    return 1
  fi
  printf 'Published image: %s\n' "$IMAGE_URI"
}

ecr_login() {
  local registry="${ECR_URI%%/*}"
  printf 'Authenticating Docker to ECR registry %s.\n' "$registry"
  if aws_profiled ecr get-login-password | "$DOCKER_CLI" login \
      --username AWS --password-stdin "$registry"; then
    return 0
  fi
  echo "ECR authentication failed before image publication; verifying whether the profile expired." >&2
  ensure_aws_identity || return 1
  echo "Retrying the read-only ECR authentication once after identity verification." >&2
  aws_profiled ecr get-login-password | "$DOCKER_CLI" login \
    --username AWS --password-stdin "$registry"
}

if [[ "$MODE" == "--build-push" ]]; then
  publish_image
  echo "Image published. ECS and CloudFormation were not changed."
  exit 0
fi

if [[ "$MODE" == "--deploy-published" ]]; then
  REPOSITORY_NAME="${ECR_URI##*/}"
  IMAGE_DIGEST="$(aws_profiled ecr describe-images --repository-name "$REPOSITORY_NAME" \
    --image-ids "imageTag=$IMAGE_TAG" --query 'imageDetails[0].imageDigest' --output text)"
  require_nonempty_value "published ECR image digest for $IMAGE_TAG" "$IMAGE_DIGEST"
  printf 'Verified existing ECR image: %s@%s\n' "$ECR_URI" "$IMAGE_DIGEST"
fi

# Read-only preflight before spending time on a build; repeat it after push to
# catch a job that started while Docker was building.
read_preflight
authorize_active_job_interruption_if_needed
if [[ "$MODE" == "--deploy-published" ]]; then
  confirm_word "DEPLOY" \
    "This will deploy the already-published image $IMAGE_URI through SAM/CloudFormation; it will not build or push Docker."
else
  confirm_word "DEPLOY" \
    "This will build and push $IMAGE_URI, then request a SAM/CloudFormation rollout."
  publish_image
fi

read_preflight
authorize_active_job_interruption_if_needed

printf 'Deploying the published image through SAM/CloudFormation: %s\n' "$IMAGE_TAG"
declare -a DEPLOY_SAM_ARGS=(--image-tag "$IMAGE_TAG")
for parameter in "${SAM_PARAMETERS[@]}"; do
  DEPLOY_SAM_ARGS+=(--parameter "$parameter")
done
"$SCRIPT_DIR/deploy-sam.sh" "${DEPLOY_SAM_ARGS[@]}"

echo "Waiting for ECS service stability through the CloudFormation-managed service."
if ! aws_profiled ecs wait services-stable \
    --cluster "$CLUSTER_NAME" --services "$SERVICE_NAME"; then
  echo "ECS did not reach stability. Service events and task diagnostics follow; no direct ECS update or rollback was attempted." >&2
  aws_profiled ecs describe-services --cluster "$CLUSTER_NAME" --services "$SERVICE_NAME" \
    --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount,taskDefinition:taskDefinition,events:events[0:10]}' \
    --output json || true
  TASK_ARNS_TEXT="$(aws_profiled ecs list-tasks --cluster "$CLUSTER_NAME" \
    --service-name "$SERVICE_NAME" --desired-status RUNNING --query 'taskArns' --output text 2>/dev/null || true)"
  if [[ -n "$TASK_ARNS_TEXT" && "$TASK_ARNS_TEXT" != "None" ]]; then
    read -r -a TASK_ARNS <<< "$TASK_ARNS_TEXT"
    aws_profiled ecs describe-tasks --cluster "$CLUSTER_NAME" --tasks "${TASK_ARNS[@]}" \
      --query 'tasks[].{taskArn:taskArn,lastStatus:lastStatus,healthStatus:healthStatus,stopCode:stopCode,stoppedReason:stoppedReason,containers:containers[].{name:name,lastStatus:lastStatus,healthStatus:healthStatus,reason:reason}}' \
      --output json || true
  fi
  aws_profiled logs tail "$LOG_GROUP" --since 10m || true
  exit 1
fi

SERVICE_JSON="$(aws_profiled ecs describe-services --cluster "$CLUSTER_NAME" \
  --services "$SERVICE_NAME" --output json)"
SERVICE_FINAL="$(python3 -c '
import json,sys
s=json.loads(sys.argv[1]).get("services", [{}])[0]
print("\t".join(str(s.get(k,"")) for k in ("desiredCount","runningCount","pendingCount","taskDefinition")))
' "$SERVICE_JSON")"
IFS=$'\t' read -r FINAL_DESIRED FINAL_RUNNING FINAL_PENDING FINAL_TASK_DEFINITION <<< "$SERVICE_FINAL"
printf 'Post-deploy service: desired=%s running=%s pending=%s taskDefinition=%s\n' \
  "$FINAL_DESIRED" "$FINAL_RUNNING" "$FINAL_PENDING" "$FINAL_TASK_DEFINITION"
if [[ "$FINAL_DESIRED" != "1" || "$FINAL_RUNNING" != "1" || "$FINAL_PENDING" != "0" ]]; then
  echo "ECS service did not reach the expected desired=1/running=1/pending=0 state." >&2
  exit 1
fi

FINAL_TASK_DEFINITION_JSON="$(aws_profiled ecs describe-task-definition \
  --task-definition "$FINAL_TASK_DEFINITION" --output json)"
FINAL_IMAGE="$(python3 -c '
import json,sys
d=json.loads(sys.argv[1]).get("taskDefinition", {})
c=[c for c in d.get("containerDefinitions", []) if c.get("name")=="document-processor"]
if len(c)!=1: raise SystemExit("Could not identify deployed document-processor container")
print(c[0].get("image", ""))
' "$FINAL_TASK_DEFINITION_JSON")"
if [[ "$FINAL_IMAGE" != "$IMAGE_URI" ]]; then
  printf 'Task Definition image mismatch: expected %s, found %s\n' "$IMAGE_URI" "$FINAL_IMAGE" >&2
  exit 1
fi

TASK_ARNS_TEXT="$(aws_profiled ecs list-tasks --cluster "$CLUSTER_NAME" \
  --service-name "$SERVICE_NAME" --desired-status RUNNING --query 'taskArns' --output text)"
if [[ -z "$TASK_ARNS_TEXT" || "$TASK_ARNS_TEXT" == "None" ]]; then
  echo "ECS reports no RUNNING task after the stability waiter." >&2
  exit 1
fi
read -r -a TASK_ARNS <<< "$TASK_ARNS_TEXT"
TASKS_JSON="$(aws_profiled ecs describe-tasks --cluster "$CLUSTER_NAME" \
  --tasks "${TASK_ARNS[@]}" --output json)"
python3 -c '
import json,sys
tasks=json.loads(sys.argv[1]).get("tasks", [])
if len(tasks)!=1: raise SystemExit("Expected exactly one running processor task")
task=tasks[0]
containers=[c for c in task.get("containers", []) if c.get("name")=="document-processor"]
if task.get("lastStatus")!="RUNNING" or len(containers)!=1 or containers[0].get("lastStatus")!="RUNNING":
    raise SystemExit("Processor task/container is not RUNNING")
health=containers[0].get("healthStatus")
if health!="HEALTHY":
    raise SystemExit("Processor container health is %s, expected HEALTHY" % (health or "UNKNOWN"))
print("Processor task passed ECS container health check.")
' "$TASKS_JSON"

echo "Recent processor startup/runtime logs (last 10 minutes):"
aws_profiled logs tail "$LOG_GROUP" --since 10m
echo "Deployment verification complete for $IMAGE_URI."
