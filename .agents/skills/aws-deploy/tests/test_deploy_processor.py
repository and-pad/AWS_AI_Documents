"""Local deployment-script tests using fake AWS, Docker, and SAM executables."""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents/skills/aws-deploy/scripts"
ECR_URI = "714687693031.dkr.ecr.us-east-2.amazonaws.com/document-ai-pipeline-dev-document-processor"


AWS_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
printf 'aws %s\n' "$*" >> "$MOCK_COMMAND_LOG"
if [[ -n "${AWS_ACCESS_KEY_ID:-}${AWS_SECRET_ACCESS_KEY:-}${AWS_SESSION_TOKEN:-}" ]]; then
  echo 'static AWS credentials leaked to CLI process' >&2
  exit 93
fi
command_name="$1 $2"
query=""
args=("$@")
if [[ "$command_name" == "configure get" ]]; then
  if [[ "${MOCK_STATIC_PROFILE_KEYS:-}" == "1" ]]; then echo 'mock-static-key'; exit 0; fi
  exit 1
fi
if [[ "$command_name" == "sts get-caller-identity" ]]; then
  if [[ "${AWS_FAIL_INITIAL_STS:-}" == "1" && ! -f "$MOCK_INITIAL_STS_FAILED" ]]; then
    touch "$MOCK_INITIAL_STS_FAILED"
    echo 'ExpiredToken' >&2
    exit 254
  fi
  if [[ "${AWS_FAIL_STS_AFTER_SAM:-}" == "1" && -f "$MOCK_SAM_SUCCESS" && ! -f "$MOCK_POST_SAM_STS_FAILED" ]]; then
    touch "$MOCK_POST_SAM_STS_FAILED"
    echo 'ExpiredToken' >&2
    exit 254
  fi
  printf '{"Account":"%s","Arn":"arn:aws:iam::%s:user/deploy-operator"}\n' \
    "${MOCK_AWS_ACCOUNT:-714687693031}" "${MOCK_AWS_ACCOUNT:-714687693031}"
  exit 0
fi
for ((i=0; i<${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "--query" ]]; then query="${args[$((i+1))]}"; fi
done
case "$command_name" in
  "cloudformation describe-stacks")
    case "$query" in
      *DocumentProcessorRepositoryUri*) echo "__ECR_URI__" ;;
      *DocumentProcessorClusterName*) echo 'document-ai-pipeline-dev' ;;
      *DocumentProcessorServiceName*) echo 'document-processor' ;;
      *DocumentQueueUrl*) echo 'https://sqs.us-east-2.amazonaws.com/714687693031/document-queue' ;;
      *JobsTableName*) echo 'document-ai-pipeline-dev-jobs' ;;
      *ResultsBucketName*) echo 'document-ai-pipeline-dev-results' ;;
      'Stacks[0].Parameters')
        cat <<'JSON'
[{"ParameterKey":"Environment","ParameterValue":"dev"},{"ParameterKey":"QueueVisibilityTimeout","ParameterValue":"900"},{"ParameterKey":"FrontendOrigin","ParameterValue":"https://dfw9n3vcykvho.cloudfront.net"},{"ParameterKey":"ProcessorMaxConcurrentJobs","ParameterValue":"1"},{"ParameterKey":"OllamaModel","ParameterValue":"qwen3-vl:8b-instruct"},{"ParameterKey":"OllamaAskModel","ParameterValue":"qwen3-vl:8b-instruct"},{"ParameterKey":"OllamaAskNumCtx","ParameterValue":"8192"},{"ParameterKey":"OllamaCoordinatorQueueWaitSeconds","ParameterValue":"120"},{"ParameterKey":"DocumentProcessorImageTag","ParameterValue":"old-tag"}]
JSON
        ;;
      'Stacks[0].StackStatus') echo "${MOCK_STACK_STATUS:-UPDATE_COMPLETE}" ;;
      *status:StackStatus*) echo "{\"status\":\"${MOCK_STACK_STATUS:-UPDATE_COMPLETE}\",\"updated\":null}" ;;
      *) echo '[]' ;;
    esac
    ;;
  "ecs describe-services")
    cat <<'JSON'
{"services":[{"desiredCount":1,"runningCount":1,"pendingCount":0,"taskDefinition":"arn:aws:ecs:us-east-2:714687693031:task-definition/document-processor:1","deployments":[{"status":"PRIMARY","rolloutState":"COMPLETED"}],"events":[]}]}
JSON
    ;;
  "sqs get-queue-attributes") echo '{"Attributes":{"ApproximateNumberOfMessages":"2","ApproximateNumberOfMessagesNotVisible":"0","VisibilityTimeout":"900"}}' ;;
  "dynamodb scan") echo '{"Items":[]}' ;;
  "ecs describe-task-definition")
    image="$__ECR_URI__:old-tag"
    if [[ -f "$MOCK_SAM_SUCCESS" && -f "$MOCK_PUSHED_IMAGE" ]]; then image="$(cat "$MOCK_PUSHED_IMAGE")"; fi
    printf '{"taskDefinition":{"containerDefinitions":[{"name":"document-processor","image":"%s","stopTimeout":60,"healthCheck":{"Command":["CMD"]}}]}}\n' "$image"
    ;;
  "ecr get-login-password") echo 'mock-ecr-password' ;;
  "ecr describe-images") echo 'sha256:published-image-digest' ;;
  "ecs wait") exit 0 ;;
  "ecs list-tasks") echo 'arn:aws:ecs:us-east-2:714687693031:task/document-ai-pipeline/abc123' ;;
  "ecs describe-tasks") echo '{"tasks":[{"lastStatus":"RUNNING","healthStatus":"HEALTHY","containers":[{"name":"document-processor","lastStatus":"RUNNING","healthStatus":"HEALTHY"}]}]}' ;;
  "logs tail") echo 'mock processor health/startup logs' ;;
  "cloudformation describe-stack-events") echo 'mock CloudFormation events' ;;
  "s3api list-objects-v2") echo '0' ;;
  *) echo "unexpected mock AWS command: $command_name" >&2; exit 90 ;;
esac
'''.replace("__ECR_URI__", ECR_URI)


DOCKER_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >> "$MOCK_COMMAND_LOG"
case "$1" in
  login) cat >/dev/null; exit 0 ;;
  build)
    if [[ "${DOCKER_FAIL:-}" == "build" ]]; then exit 21; fi
    ;;
  push)
    if [[ "${DOCKER_FAIL:-}" == "push" ]]; then exit 22; fi
    printf '%s' "$2" > "$MOCK_PUSHED_IMAGE"
    ;;
  *) echo "unexpected mock Docker command: $*" >&2; exit 91 ;;
esac
'''


SAM_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
printf 'sam %s\n' "$*" >> "$MOCK_COMMAND_LOG"
if [[ "$1" == "deploy" ]]; then
  touch "$MOCK_SAM_SUCCESS"
  if [[ "${SAM_FAIL:-}" == "deploy" ]]; then exit 31; fi
fi
'''


class DeployProcessorScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp.name)
        self.bin = self.temp_path / "bin"
        self.bin.mkdir()
        self.command_log = self.temp_path / "commands.log"
        self.pushed_image = self.temp_path / "pushed-image"
        self.sam_success = self.temp_path / "sam-success"
        self._write_executable("aws", AWS_STUB)
        self._write_executable("docker", DOCKER_STUB)
        self._write_executable("sam", SAM_STUB)
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin}:{self.env['PATH']}",
                "AWS_CLI": "aws",
                "DOCKER_CLI": "docker",
                "SAM_CLI": "sam",
                "MOCK_COMMAND_LOG": str(self.command_log),
                "MOCK_PUSHED_IMAGE": str(self.pushed_image),
                "MOCK_SAM_SUCCESS": str(self.sam_success),
                "MOCK_INITIAL_STS_FAILED": str(self.temp_path / "initial-sts-failed"),
                "MOCK_POST_SAM_STS_FAILED": str(self.temp_path / "post-sam-sts-failed"),
                "MOCK_AWS_ACCOUNT": "714687693031",
                "AWS_PROFILE": "document-ai-ohio",
                "AWS_REGION": "us-east-2",
                "STACK_NAME": "document-ai-pipeline",
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_executable(self, name: str, contents: str) -> None:
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def _run(self, *args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env.update(env_overrides)
        return subprocess.run(
            [str(SCRIPTS / "deploy-processor.sh"), *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _read_until(self, fd: int, needle: bytes, timeout: float = 45) -> bytes:
        output = bytearray()
        deadline = time.monotonic() + timeout
        while needle not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"Timed out waiting for prompt {needle!r}; output={bytes(output)!r}")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                raise AssertionError(f"Process closed before prompt {needle!r}; output={bytes(output)!r}")
            output.extend(chunk)
        return bytes(output)

    def _run_interactive(
        self,
        command: list[str],
        prompts: tuple[tuple[bytes, bytes], ...],
        **env_overrides: str,
    ) -> tuple[int, str]:
        master_fd, slave_fd = pty.openpty()
        env = self.env.copy()
        env.update(env_overrides)
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        output = bytearray()
        try:
            for prompt, response in prompts:
                output.extend(self._read_until(master_fd, prompt))
                os.write(master_fd, response)
            while process.poll() is None:
                ready, _, _ = select.select([master_fd], [], [], 0.2)
                if ready:
                    try:
                        output.extend(os.read(master_fd, 4096))
                    except OSError:
                        break
            process.wait(timeout=15)
            while True:
                ready, _, _ = select.select([master_fd], [], [], 0)
                if not ready:
                    break
                try:
                    output.extend(os.read(master_fd, 4096))
                except OSError:
                    break
            return process.returncode, output.decode(errors="replace")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master_fd)

    def _run_interactive_deploy(
        self,
        prompts: tuple[tuple[bytes, bytes], ...] | None = None,
        **env_overrides: str,
    ) -> tuple[int, str]:
        if prompts is None:
            prompts = (
                (b"Type DEPLOY to continue", b"DEPLOY\n"),
                (b"Type SAM to continue", b"SAM\n"),
            )
        return self._run_interactive(
            [str(SCRIPTS / "deploy-processor.sh"), "--deploy"],
            prompts,
            **env_overrides,
        )

    def test_build_push_resolves_ecr_and_uses_expected_docker_command(self) -> None:
        result = self._run("--build-push")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        log = self.command_log.read_text()
        self.assertIn("cloudformation describe-stacks", log)
        self.assertIn("sts get-caller-identity --profile document-ai-ohio --region us-east-2", log)
        self.assertIn("AWS identity verified: account=714687693031", result.stdout)
        self.assertIn("ecr get-login-password", log)
        match = re.search(r"processor-\d{8}T\d{6}Z", log)
        self.assertIsNotNone(match)
        tag = match.group(0)
        image = f"{ECR_URI}:{tag}"
        self.assertIn(f"docker build --platform linux/amd64 -t {image} ./document-processor", log)
        self.assertIn(f"docker push {image}", log)
        self.assertLess(log.index("docker build"), log.index("docker push"))
        self.assertNotIn("sam ", log)
        self.assertNotIn("ecs update-service", log)

    def test_docker_build_failure_stops_before_push_and_sam(self) -> None:
        result = self._run("--build-push", DOCKER_FAIL="build")
        self.assertNotEqual(result.returncode, 0)
        log = self.command_log.read_text()
        self.assertIn("docker build", log)
        self.assertNotIn("docker push", log)
        self.assertNotIn("sam ", log)

    def test_push_failure_stops_before_sam(self) -> None:
        result = self._run("--build-push", DOCKER_FAIL="push")
        self.assertNotEqual(result.returncode, 0)
        log = self.command_log.read_text()
        self.assertIn("docker push", log)
        lines = log.splitlines()
        last_sts = max(i for i, line in enumerate(lines) if "sts get-caller-identity" in line)
        push_line = next(i for i, line in enumerate(lines) if line.startswith("docker push "))
        self.assertLess(last_sts, push_line)
        self.assertNotIn("sam ", log)

    def test_deploy_passes_unique_tag_and_preserves_stack_parameters(self) -> None:
        return_code, output = self._run_interactive_deploy()
        self.assertEqual(return_code, 0, output)
        log = self.command_log.read_text()
        tag_match = re.search(r"DocumentProcessorImageTag=(processor-\d{8}T\d{6}Z)", log)
        self.assertIsNotNone(tag_match, log)
        tag = tag_match.group(1)
        assembled = str(ROOT / "infrastructure/aws/.template-assembled.yml")
        self.assertIn(f"sam validate --lint --template-file {assembled}", log)
        self.assertIn(f"sam build --template-file {assembled}", log)
        self.assertIn("Environment=dev", log)
        self.assertIn("QueueVisibilityTimeout=900", log)
        self.assertIn("FrontendOrigin=https://dfw9n3vcykvho.cloudfront.net", log)
        self.assertIn("ProcessorMaxConcurrentJobs=1", log)
        self.assertIn("OllamaModel=qwen3-vl:8b-instruct", log)
        self.assertIn("OllamaAskModel=qwen3-vl:8b-instruct", log)
        self.assertIn("OllamaAskNumCtx=8192", log)
        self.assertIn("OllamaCoordinatorQueueWaitSeconds=120", log)
        self.assertIn(f"DocumentProcessorImageTag={tag}", log)
        self.assertIn("--confirm-changeset", log)
        lines = log.splitlines()
        sam_deploy_line = next(i for i, line in enumerate(lines) if line.startswith("sam deploy "))
        self.assertLess(max(i for i, line in enumerate(lines[:sam_deploy_line]) if "sts get-caller-identity" in line), sam_deploy_line)
        self.assertIn("ecs wait services-stable", log)
        self.assertIn("ecs describe-tasks", log)
        self.assertIn("logs tail /ecs/document-ai-pipeline-dev-document-processor --since 10m", log)
        self.assertIn("passed ECS container health check", output)
        self.assertFalse(any("ecs update-service" in line for line in log.splitlines()))
        self.assertFalse(any("git commit" in line or "git push" in line for line in log.splitlines()))

    def test_deploy_published_image_reuses_ecr_tag_without_rebuild_or_push(self) -> None:
        tag = "processor-20261024T041332Z"
        image = f"{ECR_URI}:{tag}"
        self.pushed_image.write_text(image)
        return_code, output = self._run_interactive(
            [
                str(SCRIPTS / "deploy-processor.sh"),
                "--deploy-published", tag,
                "--parameter", "OllamaModel=qwen3-vl:8b-instruct",
                "--parameter", "OllamaAskNumPredict=2000",
            ],
            (
                (b"Type DEPLOY to continue", b"DEPLOY\n"),
                (b"Type SAM to continue", b"SAM\n"),
            ),
        )
        self.assertEqual(return_code, 0, output)
        log = self.command_log.read_text()
        self.assertIn("ecr describe-images", log)
        self.assertIn(f"DocumentProcessorImageTag={tag}", log)
        self.assertIn("OllamaModel=qwen3-vl:8b-instruct", log)
        self.assertIn("OllamaAskModel=qwen3-vl:8b-instruct", log)
        self.assertIn("OllamaAskNumPredict=2000", log)
        self.assertNotIn("docker build", log)
        self.assertNotIn("docker push", log)
        self.assertIn(f"--config-file {ROOT / 'infrastructure/aws/samconfig.toml'}", log)
        self.assertIn("ecs wait services-stable", log)

    def test_published_image_missing_from_ecr_stops_before_confirmation_or_sam(self) -> None:
        self._write_executable(
            "aws",
            AWS_STUB.replace(
                '"ecr describe-images") echo \'sha256:published-image-digest\' ;;',
                '"ecr describe-images") echo None ;;',
            ),
        )
        result = self._run("--deploy-published", "processor-missing")
        self.assertNotEqual(result.returncode, 0)
        log = self.command_log.read_text()
        self.assertIn("ecr describe-images", log)
        self.assertNotIn("docker build", log)
        self.assertNotIn("sam deploy", log)

    def test_deploy_can_explicitly_override_question_model_parameter(self) -> None:
        return_code, output = self._run_interactive(
            [str(SCRIPTS / "deploy-processor.sh"), "--deploy", "--parameter", "OllamaAskModel=qwen3-vl:8b-instruct"],
            ((b"Type DEPLOY to continue", b"DEPLOY\n"), (b"Type SAM to continue", b"SAM\n")),
        )
        self.assertEqual(return_code, 0, output)
        log = self.command_log.read_text()
        self.assertIn("OllamaAskModel=qwen3-vl:8b-instruct", log)
        self.assertNotIn("OllamaAskModel=deepseek-r1:8b", log)

    def test_sam_failure_reports_stack_events_and_never_updates_ecs_directly(self) -> None:
        return_code, output = self._run_interactive_deploy(SAM_FAIL="deploy")
        self.assertNotEqual(return_code, 0, output)
        log = self.command_log.read_text()
        self.assertIn("cloudformation describe-stack-events", log)
        self.assertNotIn("ecs wait services-stable", log)
        self.assertNotIn("ecs update-service", log)
        self.assertIn("no ECS service update was attempted outside CloudFormation", output)

    def test_invalid_mode_does_not_call_aws_or_docker(self) -> None:
        result = self._run("deploy")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.command_log.exists())

    def test_wrong_account_stops_before_any_write(self) -> None:
        result = self._run("--build-push", MOCK_AWS_ACCOUNT="999999999999")
        self.assertNotEqual(result.returncode, 0)
        log = self.command_log.read_text()
        self.assertIn("sts get-caller-identity", log)
        self.assertNotIn("ecr get-login-password", log)
        self.assertNotIn("docker ", log)
        self.assertIn("Wrong AWS account", result.stderr)

    def test_expired_session_shows_manual_login_command_and_stops_noninteractive(self) -> None:
        result = self._run("--build-push", AWS_FAIL_INITIAL_STS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("aws login --remote", result.stderr)
        self.assertIn("--profile document-ai-ohio", result.stderr)
        self.assertIn("--region us-east-2", result.stderr)
        log = self.command_log.read_text()
        self.assertNotIn("aws login", log)
        self.assertNotIn("ecr get-login-password", log)
        self.assertNotIn("docker ", log)

    def test_static_credential_environment_is_discarded(self) -> None:
        result = self._run(
            "--build-push",
            AWS_ACCESS_KEY_ID="not-a-real-key",
            AWS_SECRET_ACCESS_KEY="not-a-real-secret",
            AWS_SESSION_TOKEN="not-a-real-session",
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_static_keys_in_named_profile_are_rejected(self) -> None:
        result = self._run("--build-push", MOCK_STATIC_PROFILE_KEYS="1")
        self.assertNotEqual(result.returncode, 0)
        log = self.command_log.read_text()
        self.assertIn("configure get aws_access_key_id --profile document-ai-ohio", log)
        self.assertNotIn("sts get-caller-identity", log)
        self.assertNotIn("ecr get-login-password", log)

    def test_user_refreshes_expired_session_and_identity_is_reverified(self) -> None:
        code, output = self._run_interactive(
            [str(SCRIPTS / "deploy-processor.sh"), "--build-push"],
            ((b"press Enter here to recheck identity", b"\n"),),
            AWS_FAIL_INITIAL_STS="1",
        )
        self.assertEqual(code, 0, output)
        log = self.command_log.read_text()
        self.assertEqual(log.count("sts get-caller-identity"), 3)
        self.assertIn("docker push", log)

    def test_expiration_during_sam_checks_stack_without_repeating_writes(self) -> None:
        code, output = self._run_interactive_deploy(
            prompts=(
                (b"Type DEPLOY to continue", b"DEPLOY\n"),
                (b"Type SAM to continue", b"SAM\n"),
                (b"press Enter here to recheck identity", b"\n"),
            ),
            SAM_FAIL="deploy",
            AWS_FAIL_STS_AFTER_SAM="1",
            MOCK_STACK_STATUS="UPDATE_ROLLBACK_COMPLETE",
        )
        self.assertNotEqual(code, 0, output)
        log = self.command_log.read_text()
        self.assertEqual(sum(line.startswith("sam deploy ") for line in log.splitlines()), 1)
        self.assertIn("cloudformation describe-stacks", log)
        self.assertIn("cloudformation describe-stack-events", log)
        self.assertNotIn("ecs wait services-stable", log)
        self.assertIn("aws login --remote", output)
        self.assertIn("UPDATE_ROLLBACK_COMPLETE", output)


if __name__ == "__main__":
    unittest.main()
