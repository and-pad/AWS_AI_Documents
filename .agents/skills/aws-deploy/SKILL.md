---
name: aws-deploy
description: Build, validate, and deploy AWS_AI_Documents through its SAM and CloudFormation-managed release procedures.
---

# AWS deployment workflow

Use this skill only for deployment work the user has requested. Do not deploy,
publish an image, update frontend assets, or invalidate CloudFront without that
explicit request.

## AWS authentication is interactive and user-controlled

The deployment profile is `document-ai-ohio`, region is `us-east-2`, expected
account is `714687693031`, and stack is `document-ai-pipeline`.

Before any AWS write, run and validate:

```bash
aws sts get-caller-identity \
  --profile document-ai-ohio \
  --region us-east-2
```

Proceed only when the returned `Account` is exactly `714687693031`. If STS fails
or the credentials have expired, stop before writes and show the user this
command to execute from their own terminal:

```bash
aws login --remote \
  --profile document-ai-ohio \
  --region us-east-2
```

Never automate the browser, choose an account, enter a password, or complete
MFA. After the user confirms they finished the command, run
`aws sts get-caller-identity` again with the same profile and region. Continue
only if the identity is valid and the account is still `714687693031`. If there
is no interactive terminal, stop and ask the user to refresh credentials and
rerun the script. Do not use `aws sso login` or static credentials. Scripts
must not store session tokens, credentials, or secrets; the project deploy
helpers discard static credential environment variables and use the named
profile. The helpers also reject access-key fields in that named profile without
printing their values.

Revalidate identity immediately before ECR image publication and before SAM
creates a CloudFormation changeset. If credentials fail during a write, do not
repeat that write automatically. Once the user refreshes credentials, verify
identity and inspect the actual CloudFormation stack status and recent events
before deciding whether another deployment is necessary. An in-progress or
completed changeset may already have taken effect.

## Choose the matching helper

- `scripts/assemble.sh`: combine `infrastructure/aws/template.yml` and its
  fragments into `.template-assembled.yml`.
- `scripts/validate.sh`: assemble and run `sam validate --lint`; no deployment.
- `scripts/build.sh`: assemble, lint, and run `sam build`; no deployment.
- `scripts/deploy-sam.sh`: deploy SAM/CloudFormation changes that do not publish
  a new `document-processor` image. It carries forward the current stack
  parameters and prompts before creating a changeset; SAM then presents its
  changeset confirmation. Review the resource changes before approving. Use
  `--parameter OllamaAskModel=qwen3-vl:8b-instruct` when applying the question
  model because existing live parameter values otherwise override template
  defaults.
- `scripts/deploy-processor.sh --build-push`: build and publish one uniquely
  tagged processor image to ECR only. It does not change ECS or CloudFormation.
- `scripts/deploy-processor.sh --deploy`: preflight, build and push one image,
  pass that exact tag to SAM, set `OllamaAskModel=qwen3-vl:8b-instruct` by default,
  and verify CloudFormation/ECS, health, and logs. Use `--parameter KEY=VALUE`
  to override a SAM parameter explicitly.
- `scripts/deploy-processor.sh --deploy-published TAG`: resume after an image
  push succeeded but SAM did not create/apply its changeset. It verifies that
  exact tag in ECR, repeats the active-job preflight, and deploys through SAM
  without rebuilding or pushing.
- `scripts/status.sh`: read-only CloudFormation, ECS, and document-queue status.
- `scripts/deploy-frontend.sh`: build and publish the SPA to its configured S3
  bucket, then request invalidation of its CloudFront distribution. Use only
  for an explicitly requested frontend release; it is independent of the
  processor image workflow.

**For changes under `document-processor/`, use `deploy-processor.sh`. Never
deploy a new processor image with `sam deploy` alone: build and publish the
image to ECR first, then update `DocumentProcessorImageTag` through SAM.** ECS
and its Task Definition are CloudFormation-managed. Never use
`aws ecs update-service` to change the image or bypass CloudFormation.

Before deploying the dual-model coordinator integration, install and verify
the host coordinator and manually switch the Apache `/api/chat` upstream to
`127.0.0.1:11435` while retaining TLS and Bearer authentication. Validate and
reload Apache on the Ollama host first; AWS deployment does not modify that
host. Without this step, AWS requests would still bypass the coordinator.

## Processor image release

The processor release uses these project values:

- ECR URI: CloudFormation output `DocumentProcessorRepositoryUri`.
- ECS cluster/service: `document-ai-pipeline-dev` / `document-processor` (the
  helper resolves the stack outputs instead of hardcoding them for operations).
- Docker platform/context: `linux/amd64` / `./document-processor`.
- Image tag: `processor-$(date -u +%Y%m%dT%H%M%SZ)`.
- SAM parameter: `DocumentProcessorImageTag`.

Run from the repository root:

```bash
.agents/skills/aws-deploy/scripts/deploy-processor.sh --build-push
```

This builds and pushes only. For the full deployment:

```bash
.agents/skills/aws-deploy/scripts/deploy-processor.sh --deploy
```

The processor deployment also updates the question worker's SAM code and
explicitly selects `qwen3-vl:8b-instruct`; this matters because the deployment helper
preserves existing stack parameters instead of automatically adopting template
defaults. A different question model can be selected explicitly with
`--parameter OllamaAskModel=MODEL`.

The full mode first verifies AWS identity/account and performs read-only
preflight checks for ECS stability, current container health and stop timeout,
`PROCESSING` DynamoDB jobs, approximate in-flight/visible SQS counts, and
checkpoints for active jobs. It never receives or deletes queue messages. If
jobs or in-flight messages exist, it requires the operator to type `INTERRUPT`
to acknowledge the risk before proceeding. The worker drains for at most 45
seconds and the container `stopTimeout` is read from the active Task Definition
(currently 60 seconds); a longer Ollama call can be interrupted. The message
remains available for SQS retry and confirmed checkpoints are retained, but
the current page may need to run again. Do not authorize this interruption
unless it is acceptable.

Before ECR push, the helper confirms identity again, obtains an ECR login token,
builds exactly once, refreshes the ECR login token, confirms identity again,
and pushes the same tag. A build failure stops before push and SAM. A push
failure stops before SAM; it is not retried automatically and the tag is not
deleted. SAM assembles the template, runs lint validation and `sam build`,
reads live CloudFormation parameters, changes only the processor image tag, and
invokes the existing `samconfig.toml` deployment configuration. No Docker image
is rebuilt by SAM.

The helper asks for `DEPLOY` before image publication. Before SAM creates the
CloudFormation changeset, it asks for `SAM`; after SAM displays the changeset,
the SAM confirmation prompt remains enabled. Review the changeset and continue
only when it contains the expected Task Definition revision and ECS service
rollout, with no unexpected stateful resource replacement. Declining or
failing SAM leaves the pushed tag available and does not trigger a direct ECS
update or automatic rollback.

After CloudFormation completes, the helper runs the ECS `services-stable`
waiter, checks `desiredCount=1`, `runningCount=1`, `pendingCount=0`, verifies
the active Task Definition references the exact pushed image, checks the
container health status, and tails the processor log group for the last ten
minutes. A RUNNING task alone is not considered sufficient confirmation.
If ECS does not stabilize, the helper prints service events, task diagnostics,
and recent logs. It does not perform destructive recovery actions.

## Failed or interrupted deployment

If Docker build or push fails, stop at that stage. If SAM fails, inspect
CloudFormation events and preserve the ECR tag. If credentials expire while
SAM is running, the helper does not retry `sam deploy`: after interactive
credential refresh it re-verifies account identity, reads stack status/events,
and exits so the operator can decide based on the actual state. If ECS fails
to stabilize, inspect the printed ECS events and logs before deciding the next
step. Never resolve a failed rollout by changing the service or Task Definition
outside CloudFormation.

If the image is already published, resume with that tag rather than publishing
another image:

```bash
.agents/skills/aws-deploy/scripts/deploy-processor.sh \
  --deploy-published processor-YYYYMMDDTHHMMSSZ \
  --parameter OllamaModel=qwen3-vl:8b-instruct \
  --parameter OllamaAskNumPredict=2000
```

The processor deployment path does not build or publish React assets. A
processor release must not update the frontend bucket or invalidate CloudFront.

## Frontend release

For an explicitly authorized frontend release, run:

```bash
.agents/skills/aws-deploy/scripts/deploy-frontend.sh
```

The script verifies the AWS identity and expected account, builds from
`frontend/` with the configured production API and Cognito identifiers, checks
for `frontend/dist/index.html`, and asks for the exact confirmation `PUBLISH`
before AWS writes. It rechecks identity before syncing to
`s3://document-ai-frontend-714687693031-us-east-2-an/` with `--delete`, then
rechecks again before requesting invalidation `/*` for distribution
`E2W9M0G33GE6HK`. A failed sync or invalidation is not automatically repeated.
