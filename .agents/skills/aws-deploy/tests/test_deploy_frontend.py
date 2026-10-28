"""Frontend release tests using fake npm and AWS CLI executables only."""

from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".agents/skills/aws-deploy/scripts/deploy-frontend.sh"
COMMON = ROOT / ".agents/skills/aws-deploy/scripts/aws-deploy-common.sh"
EXPECTED_BUCKET = "s3://document-ai-frontend-714687693031-us-east-2-an/"
EXPECTED_DISTRIBUTION = "E2W9M0G33GE6HK"


AWS_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
printf 'aws %s\n' "$*" >> "$MOCK_COMMAND_LOG"
if [[ -n "${AWS_ACCESS_KEY_ID:-}${AWS_SECRET_ACCESS_KEY:-}${AWS_SESSION_TOKEN:-}" ]]; then
  echo 'static AWS credentials leaked to CLI process' >&2
  exit 93
fi
command_name="$1 $2"
if [[ "$command_name" == "configure get" ]]; then exit 1; fi
if [[ "$command_name" == "sts get-caller-identity" ]]; then
  if [[ "${AWS_FAIL_STS:-}" == "1" ]]; then echo 'ExpiredToken' >&2; exit 254; fi
  printf '{"Account":"%s","Arn":"arn:aws:iam::%s:user/deploy-operator"}\n' \
    "${MOCK_AWS_ACCOUNT:-714687693031}" "${MOCK_AWS_ACCOUNT:-714687693031}"
  exit 0
fi
case "$command_name" in
  "s3 sync")
    if [[ "${AWS_FAIL_S3_SYNC:-}" == "1" ]]; then exit 41; fi
    ;;
  "cloudfront create-invalidation")
    if [[ "${AWS_FAIL_INVALIDATION:-}" == "1" ]]; then exit 42; fi
    echo '{"Invalidation":{"Id":"I-MOCK","Status":"InProgress"}}'
    ;;
  "cloudfront list-invalidations") echo '{"InvalidationList":{"Items":[]}}' ;;
  *) echo "unexpected mock AWS command: $command_name" >&2; exit 90 ;;
esac
'''


NPM_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
printf 'npm %s cwd=%s api=%s pool=%s client=%s\n' "$*" "$PWD" \
  "${VITE_API_BASE_URL:-}" "${VITE_COGNITO_USER_POOL_ID:-}" \
  "${VITE_COGNITO_USER_POOL_CLIENT_ID:-}" >> "$MOCK_COMMAND_LOG"
[[ "$*" == "run build" ]] || exit 91
if [[ "${NPM_FAIL:-}" == "1" ]]; then exit 31; fi
mkdir -p dist
if [[ "${NPM_OMIT_INDEX:-}" != "1" ]]; then printf '<html>mock</html>\n' > dist/index.html; fi
'''


class DeployFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "project"
        self.scripts = self.repo / ".agents/skills/aws-deploy/scripts"
        self.scripts.mkdir(parents=True)
        shutil.copy2(SCRIPT, self.scripts / "deploy-frontend.sh")
        shutil.copy2(COMMON, self.scripts / "aws-deploy-common.sh")
        (self.repo / "frontend").mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self._write_executable("aws", AWS_STUB)
        self._write_executable("npm", NPM_STUB)
        self.command_log = self.base / "commands.log"
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin}:{self.env['PATH']}",
                "AWS_CLI": "aws",
                "AWS_PROFILE": "document-ai-ohio",
                "AWS_REGION": "us-east-2",
                "MOCK_COMMAND_LOG": str(self.command_log),
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_executable(self, name: str, contents: str) -> None:
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def _run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env.update(overrides)
        return subprocess.run(
            [str(self.scripts / "deploy-frontend.sh")],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _read_until(self, fd: int, needle: bytes, timeout: float = 30) -> bytes:
        output = bytearray()
        deadline = time.monotonic() + timeout
        while needle not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"Timed out waiting for {needle!r}; output={bytes(output)!r}")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                raise AssertionError(f"Process closed before {needle!r}; output={bytes(output)!r}") from exc
            if not chunk:
                raise AssertionError(f"Process closed before {needle!r}; output={bytes(output)!r}")
            output.extend(chunk)
        return bytes(output)

    def _run_approved(self, response: bytes = b"PUBLISH\n", **overrides: str) -> tuple[int, str]:
        env = self.env.copy()
        env.update(overrides)
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            [str(self.scripts / "deploy-frontend.sh")],
            cwd=self.repo,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        output = bytearray()
        try:
            output.extend(self._read_until(master_fd, b"Type PUBLISH to continue"))
            os.write(master_fd, response)
            while process.poll() is None:
                ready, _, _ = select.select([master_fd], [], [], 0.2)
                if ready:
                    try:
                        output.extend(os.read(master_fd, 4096))
                    except OSError:
                        break
            process.wait(timeout=10)
            return process.returncode, output.decode(errors="replace")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master_fd)

    def test_build_uses_production_variables_and_publishes_then_invalidates(self) -> None:
        code, output = self._run_approved()
        self.assertEqual(code, 0, output)
        log = self.command_log.read_text()
        npm_line = next(line for line in log.splitlines() if line.startswith("npm "))
        self.assertIn(f"cwd={self.repo}/frontend", npm_line)
        self.assertIn("api=https://prvyiqstbc.execute-api.us-east-2.amazonaws.com/dev", npm_line)
        self.assertIn("pool=us-east-2_wwDVGjsAN", npm_line)
        self.assertIn("client=4vie26va19mb50d227c6j9amqo", npm_line)
        sync_line = next(line for line in log.splitlines() if line.startswith("aws s3 sync "))
        self.assertIn(f"s3 sync {self.repo}/frontend/dist/ {EXPECTED_BUCKET} --delete", sync_line)
        self.assertIn("--profile document-ai-ohio --region us-east-2", sync_line)
        invalidation_line = next(line for line in log.splitlines() if "cloudfront create-invalidation" in line)
        self.assertIn(f"--distribution-id {EXPECTED_DISTRIBUTION} --paths /*", invalidation_line)
        self.assertLess(log.index(sync_line), log.index(invalidation_line))
        sts_lines = [i for i, line in enumerate(log.splitlines()) if "sts get-caller-identity" in line]
        self.assertGreaterEqual(len(sts_lines), 3)
        self.assertLess(sts_lines[1], log.splitlines().index(sync_line))
        self.assertLess(sts_lines[2], log.splitlines().index(invalidation_line))

    def test_rejecting_publish_confirmation_prevents_aws_writes(self) -> None:
        code, _ = self._run_approved(response=b"NO\n")
        self.assertNotEqual(code, 0)
        log = self.command_log.read_text()
        self.assertFalse(any(line.startswith("aws s3 sync ") for line in log.splitlines()))
        self.assertFalse(any("cloudfront create-invalidation" in line for line in log.splitlines()))

    def test_failed_build_stops_before_publication(self) -> None:
        result = self._run(NPM_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        log = self.command_log.read_text()
        self.assertFalse(any(line.startswith("aws s3 sync ") for line in log.splitlines()))
        self.assertFalse(any("cloudfront create-invalidation" in line for line in log.splitlines()))

    def test_missing_index_stops_before_publication(self) -> None:
        result = self._run(NPM_OMIT_INDEX="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not produce frontend/dist/index.html", result.stderr)
        log = self.command_log.read_text()
        self.assertFalse(any(line.startswith("aws s3 sync ") for line in log.splitlines()))

    def test_wrong_account_stops_before_build_and_writes(self) -> None:
        result = self._run(MOCK_AWS_ACCOUNT="123456789012")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Wrong AWS account", result.stderr)
        log = self.command_log.read_text()
        self.assertFalse(any(line.startswith("npm ") for line in log.splitlines()))
        self.assertFalse(any(line.startswith("aws s3 sync ") for line in log.splitlines()))

    def test_expired_noninteractive_session_displays_manual_login_and_stops(self) -> None:
        result = self._run(AWS_FAIL_STS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("aws login --remote", result.stderr)
        self.assertIn("--profile document-ai-ohio", result.stderr)
        self.assertIn("--region us-east-2", result.stderr)
        log = self.command_log.read_text()
        self.assertFalse(any(line.startswith("npm ") for line in log.splitlines()))
        self.assertFalse(any(line.startswith("aws s3 sync ") for line in log.splitlines()))

    def test_failed_s3_sync_does_not_invalidate_or_retry(self) -> None:
        code, output = self._run_approved(AWS_FAIL_S3_SYNC="1")
        self.assertNotEqual(code, 0, output)
        log = self.command_log.read_text()
        self.assertEqual(sum(line.startswith("aws s3 sync ") for line in log.splitlines()), 1)
        self.assertFalse(any("cloudfront create-invalidation" in line for line in log.splitlines()))

    def test_failed_invalidation_is_not_retried_and_is_inspected_read_only(self) -> None:
        code, output = self._run_approved(AWS_FAIL_INVALIDATION="1")
        self.assertNotEqual(code, 0, output)
        log = self.command_log.read_text()
        self.assertEqual(sum("cloudfront create-invalidation" in line for line in log.splitlines()), 1)
        self.assertIn("cloudfront list-invalidations", log)


if __name__ == "__main__":
    unittest.main()
