"""Safe, request-local correlation and structured diagnostic events."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any


_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("processor_diagnostic_context", default={})

# Values are deliberately allow-listed so accidental prompt/document fields do
# not enter normal CloudWatch events through a generic logging helper.
_SAFE_FIELDS = {
    "job_id", "sqs_message_id", "sqs_receive_count", "processing_attempt", "ecs_task_id",
    "processing_owner", "worker_slot", "active_jobs", "max_concurrent_jobs",
    "consumer_count", "consumers_still_alive",
    "processing_stage", "page_number", "total_pages", "ollama_request_id", "model",
    "message_count", "image_count", "image_width", "image_height", "image_format", "max_receive_count",
    "image_bytes", "image_sha256", "render_elapsed_ms", "prompt_length", "context_summary_length",
    "schema_name", "attempt", "retry_attempt", "num_ctx", "num_predict", "timeout_seconds",
    "connect_timeout_seconds",
    "elapsed_ms", "request_elapsed_ms", "http_status", "content_type", "done", "done_reason",
    "truncation_status", "read_timeout",
    "prompt_eval_count", "eval_count", "prompt_eval_duration", "eval_duration", "total_duration",
    "load_duration", "response_content_length", "response_content_sha256", "thinking_length",
    "json_valid", "schema_valid", "validation_error_category", "error_type", "error_category",
    "api_error_present",
    "error_count", "field_path", "parser_position", "parser_message", "status", "progress_percent",
    "sent_timestamp", "visibility_timeout_seconds", "heartbeat_interval_seconds", "renewal_count",
    "duration_ms", "lease_persisted", "receive_wait_seconds", "message_count_received", "outcome",
    "exception_category", "aws_error_code", "sqs_message_deleted", "ecs_task_stopping",
    "drain_timeout_seconds",
    "record_count", "extraction_count", "total_transcription_chars", "visual_elements_count",
    "pages_rendered", "job_status", "s3_result_verified", "pdf_deleted", "shutdown_reason",
    "raw_length", "failure_stage", "retry_scheduled", "message_received", "lease_age_seconds",
    "pdf_bytes", "result_bytes",
    "checkpoint_key", "schema_version", "recovered_pages", "checkpoint_count",
}


def _filtered(values: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in values.items():
        if key not in _SAFE_FIELDS or value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            # All free-form strings passed through this path are identifiers,
            # categories, hashes or fixed enum-like values, never document data.
            result[key] = value
    return result


@lru_cache(maxsize=1)
def ecs_task_id() -> str | None:
    """Read the task ARN from ECS's local metadata endpoint once per task."""
    metadata_uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4")
    if not metadata_uri:
        return os.getenv("ECS_TASK_ID")
    try:
        with urllib.request.urlopen(f"{metadata_uri}/task", timeout=0.25) as response:
            payload = json.loads(response.read(64 * 1024))
        arn = payload.get("TaskARN", "") if isinstance(payload, dict) else ""
        return arn.rsplit("/", 1)[-1] if arn else None
    except Exception:
        # Metadata is optional and must never prevent queue processing.
        return os.getenv("ECS_TASK_ID")


@contextmanager
def diagnostic_context(**fields):
    merged = dict(_CONTEXT.get())
    merged.update(_filtered(fields))
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_diagnostic_context() -> dict[str, Any]:
    return dict(_CONTEXT.get())


def current_job_id() -> str:
    return str(_CONTEXT.get().get("job_id", "local"))


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
        **_filtered(_CONTEXT.get()),
        **_filtered(fields),
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def log_sensitive_model_output(
    logger: logging.Logger,
    *,
    response_content: str,
    validation: dict[str, Any],
) -> None:
    """Emit raw model content only under explicit, temporary debug opt-in.

    This goes to the existing private ECS log group, whose retention is managed
    by CloudFormation. No image, request headers, token or prompt is accepted.
    """
    enabled = os.getenv("DEBUG_MODEL_OUTPUT", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": "OLLAMA_DIAGNOSTIC_RAW_RESPONSE",
        **_filtered(_CONTEXT.get()),
        **_filtered(validation),
        "response_content": response_content,
        "response_content_bytes": len(response_content.encode("utf-8", errors="replace")),
    }
    logger.warning(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


@contextmanager
def document_context(job_id: str, **fields):
    with diagnostic_context(job_id=job_id, **fields):
        yield
