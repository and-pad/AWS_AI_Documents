"""Long-poll SQS consumer run inside the Fargate document-processor task.

The queue remains the durable unit of work.  A DynamoDB lease protects against
at-least-once delivery during ECS deployments while SQS visibility is renewed
independently of a long Ollama call.
"""
from __future__ import annotations

import json
import hashlib
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

from app.document_service import process_pdf, result_pipeline_fingerprint
from app.checkpoints import S3PageCheckpointStore
from app.settings import get_settings
from app.diagnostics import (
    current_diagnostic_context,
    diagnostic_context,
    document_context,
    ecs_task_id,
    log_event,
)

logger = logging.getLogger(__name__)


class WorkerShutdown(RuntimeError):
    """Internal signal used to retain the current SQS message on shutdown."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job_id_from_key(key: str) -> str:
    """Accept owner-scoped and legacy document keys."""
    parts = key.split("/")
    if len(parts) >= 5 and parts[0] == "users" and parts[2] == "jobs":
        return parts[3]
    if len(parts) >= 3 and parts[0] == "jobs":
        return parts[1]
    return ""


def _safe_error(exc: Exception) -> str:
    """Persist a stable message without document/model content."""
    if isinstance(exc, WorkerShutdown):
        return "El procesamiento se interrumpió; se reintentará"
    if getattr(exc, "category", None) in {
        "output_limit_reached", "output_incomplete", "possible_output_limit",
    }:
        return "La salida de Ollama alcanzó el límite de tokens o quedó incompleta"
    return "El procesamiento falló; se reintentará"


class VisibilityHeartbeat:
    """Keeps one received message invisible while a synchronous VLM call runs."""

    def __init__(
        self,
        worker: "SqsDocumentWorker",
        receipt_handle: str,
        job_id: str,
        message_id: str,
        processing_owner: str,
        worker_slot: int,
    ):
        self.worker = worker
        self.receipt_handle = receipt_handle
        self.job_id = job_id
        self.message_id = message_id
        self.processing_owner = processing_owner
        self.worker_slot = worker_slot
        self.context = {
            **current_diagnostic_context(),
            "processing_owner": processing_owner,
            "worker_slot": worker_slot,
            "max_concurrent_jobs": worker.max_concurrent_jobs,
        }
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"sqs-visibility-{job_id}", daemon=True)
        self._started = time.monotonic()
        self._renewal_count = 0

    def start(self) -> None:
        with diagnostic_context(**self.context):
            log_event(
                logger,
                "SQS_VISIBILITY_HEARTBEAT_STARTED",
                visibility_timeout_seconds=self.worker.visibility_timeout,
                heartbeat_interval_seconds=self.worker.visibility_heartbeat_seconds,
            )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        with diagnostic_context(**self.context):
            log_event(
                logger,
                "SQS_VISIBILITY_HEARTBEAT_STOPPED" if not self._thread.is_alive() else "SQS_VISIBILITY_HEARTBEAT_STOP_TIMEOUT",
                level=logging.INFO if not self._thread.is_alive() else logging.WARNING,
                renewal_count=self._renewal_count,
                duration_ms=round((time.monotonic() - self._started) * 1000),
            )

    def _run(self) -> None:
        while not self._stop.wait(self.worker.visibility_heartbeat_seconds):
            try:
                with diagnostic_context(**self.context):
                    self.worker._renew_visibility(
                        self.receipt_handle, self.job_id, self.message_id, self.processing_owner
                    )
                self._renewal_count += 1
            except Exception as exc:
                error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                category = "visibility_expiration_suspected" if error_code in {
                    "ReceiptHandleIsInvalid", "MessageNotInflight"
                } else "heartbeat_renewal_failed"
                with diagnostic_context(**self.context):
                    log_event(
                        logger,
                        "SQS_VISIBILITY_HEARTBEAT_FAILED",
                        level=logging.ERROR,
                        error_category=category,
                        error_type=type(exc).__name__,
                        aws_error_code=error_code,
                        renewal_count=self._renewal_count,
                        duration_ms=round((time.monotonic() - self._started) * 1000),
                    )


class SqsDocumentWorker:
    """Run one independent long-poll consumer per configured job slot."""

    def __init__(
        self,
        *,
        settings: dict | None = None,
        sqs_client=None,
        s3_client=None,
        table=None,
        processor=process_pdf,
    ):
        config = settings or get_settings()
        self.queue_url = config["document_queue_url"]
        self.documents_bucket = config["documents_bucket"]
        self.results_bucket = config["results_bucket"]
        self.jobs_table_name = config["jobs_table"]
        self.wait_time_seconds = config["sqs_wait_time_seconds"]
        self.visibility_timeout = config["sqs_visibility_timeout"]
        self.visibility_heartbeat_seconds = config["sqs_visibility_heartbeat_seconds"]
        self.max_receive_count = config["sqs_max_receive_count"]
        self.max_concurrent_jobs = int(config.get("processor_max_concurrent_jobs", 1))
        if not 1 <= self.max_concurrent_jobs <= 2:
            raise ValueError("processor_max_concurrent_jobs must be 1 or 2")
        self.ecs_task_id = ecs_task_id()
        # Compose/local API mode has no queue configuration. Delay SDK client
        # construction in that mode so starting `/health` never probes AWS
        # credentials or metadata services.
        self.sqs = sqs_client
        self.s3 = s3_client
        self._provided_table = table
        self._thread_resources = threading.local()
        if self.enabled:
            self.sqs = self.sqs or boto3.client("sqs")
            self.s3 = self.s3 or boto3.client("s3")
        self.processor = processor
        self._stop = threading.Event()
        self._threads: dict[int, threading.Thread] = {}
        self._active_jobs: dict[int, dict[str, Any]] = {}
        self._active_lock = threading.Lock()

    @property
    def table(self):
        """Use a DynamoDB resource per thread; boto3 resources are not thread-safe."""
        if self._provided_table is not None:
            return self._provided_table
        if not self.enabled:
            return None
        table = getattr(self._thread_resources, "table", None)
        if table is None:
            session = boto3.session.Session()
            resource = session.resource("dynamodb")
            table = resource.Table(self.jobs_table_name)
            self._thread_resources.session = session
            self._thread_resources.resource = resource
            self._thread_resources.table = table
        return table

    def _active_job_count(self) -> int:
        with self._active_lock:
            return len(self._active_jobs)

    def _register_active_job(self, worker_slot: int, job_id: str, message_id: str) -> int:
        with self._active_lock:
            self._active_jobs[worker_slot] = {"job_id": job_id, "sqs_message_id": message_id}
            return len(self._active_jobs)

    def _release_active_job(self, worker_slot: int, message_id: str) -> int:
        with self._active_lock:
            active = self._active_jobs.get(worker_slot)
            if active and active.get("sqs_message_id") == message_id:
                self._active_jobs.pop(worker_slot, None)
            return len(self._active_jobs)

    def _set_active_owner(self, worker_slot: int, message_id: str, processing_owner: str) -> None:
        with self._active_lock:
            active = self._active_jobs.get(worker_slot)
            if active and active.get("sqs_message_id") == message_id:
                active["processing_owner"] = processing_owner

    @property
    def enabled(self) -> bool:
        return bool(self.queue_url and self.documents_bucket and self.results_bucket and self.jobs_table_name)

    def start(self) -> None:
        if not self.enabled:
            log_event(logger, "SQS_WORKER_DISABLED", outcome="configuration_incomplete")
            return
        if any(thread.is_alive() for thread in self._threads.values()):
            return
        self._stop.clear()
        self._threads = {}
        for worker_slot in range(1, self.max_concurrent_jobs + 1):
            thread = threading.Thread(
                target=self._run_consumer_supervised,
                kwargs={"worker_slot": worker_slot},
                name=f"sqs-document-worker-{worker_slot}",
                daemon=True,
            )
            self._threads[worker_slot] = thread
            thread.start()
        log_event(
            logger,
            "SQS_WORKER_STARTED",
            ecs_task_id=self.ecs_task_id,
            receive_wait_seconds=self.wait_time_seconds,
            visibility_timeout_seconds=self.visibility_timeout,
            heartbeat_interval_seconds=self.visibility_heartbeat_seconds,
            max_receive_count=self.max_receive_count,
            max_concurrent_jobs=self.max_concurrent_jobs,
            consumer_count=self.max_concurrent_jobs,
        )

    @property
    def health_snapshot(self) -> dict[str, Any]:
        """Report consumer liveness without treating long-running work as unhealthy."""
        if not self.enabled:
            return {
                "status": "ok",
                "sqs_worker": {
                    "enabled": False,
                    "expected_consumers": 0,
                    "healthy_consumers": 0,
                    "dead_worker_slots": [],
                    "active_jobs": 0,
                },
            }

        expected_slots = set(range(1, self.max_concurrent_jobs + 1))
        alive_slots = {
            slot for slot, thread in self._threads.items()
            if thread.is_alive()
        }
        dead_slots = sorted(expected_slots - alive_slots)
        healthy = not self._stop.is_set() and not dead_slots
        return {
            "status": "ok" if healthy else "unhealthy",
            "sqs_worker": {
                "enabled": True,
                "expected_consumers": self.max_concurrent_jobs,
                "healthy_consumers": len(alive_slots & expected_slots),
                "dead_worker_slots": dead_slots,
                "active_jobs": self._active_job_count(),
            },
        }

    def _run_consumer_supervised(self, *, worker_slot: int) -> None:
        """Make unexpected consumer exits visible to ECS through `/health`."""
        outcome = "shutdown" if self._stop.is_set() else "unexpected_exit"
        try:
            self.run_forever(worker_slot=worker_slot)
            outcome = "shutdown" if self._stop.is_set() else "unexpected_exit"
        except Exception as exc:
            outcome = "crashed"
            log_event(
                logger,
                "SQS_CONSUMER_CRASHED",
                level=logging.ERROR,
                ecs_task_id=self.ecs_task_id,
                worker_slot=worker_slot,
                active_jobs=self._active_job_count(),
                max_concurrent_jobs=self.max_concurrent_jobs,
                error_type=type(exc).__name__,
                outcome=outcome,
            )
        finally:
            log_event(
                logger,
                "SQS_CONSUMER_STOPPED",
                level=logging.WARNING if outcome != "shutdown" else logging.INFO,
                ecs_task_id=self.ecs_task_id,
                worker_slot=worker_slot,
                active_jobs=self._active_job_count(),
                max_concurrent_jobs=self.max_concurrent_jobs,
                outcome=outcome,
            )

    def stop(self, *, drain_timeout_seconds: float = 45) -> None:
        """Stop new polls and briefly drain active jobs within ECS's 60s stop window.

        Active jobs keep their SQS and DynamoDB heartbeats while draining. If the
        bounded wait expires, ECS may terminate the process; the message remains
        undeleted and becomes recoverable after its visibility/lease expires.
        """
        self._stop.set()
        log_event(
            logger,
            "SQS_WORKER_STOPPING",
            ecs_task_id=self.ecs_task_id,
            ecs_task_stopping=True,
            active_jobs=self._active_job_count(),
            max_concurrent_jobs=self.max_concurrent_jobs,
        )
        with self._active_lock:
            active_jobs = [
                {"worker_slot": slot, **active}
                for slot, active in self._active_jobs.items()
            ]
        for active in active_jobs:
            log_event(
                logger,
                "JOB_SHUTDOWN_IN_FLIGHT_RETAINED",
                job_id=active["job_id"],
                sqs_message_id=active["sqs_message_id"],
                processing_owner=active.get("processing_owner"),
                ecs_task_id=self.ecs_task_id,
                worker_slot=active["worker_slot"],
                active_jobs=len(active_jobs),
                max_concurrent_jobs=self.max_concurrent_jobs,
                outcome="keep_visibility_and_lease_until_processing_stops_or_task_exits",
            )
        deadline = time.monotonic() + max(0, drain_timeout_seconds)
        for thread in self._threads.values():
            thread.join(timeout=max(0, deadline - time.monotonic()))
        still_alive = sum(thread.is_alive() for thread in self._threads.values())
        log_event(
            logger,
            "SQS_WORKER_STOPPED" if not still_alive else "SQS_WORKER_STOP_WAIT_EXPIRED",
            level=logging.INFO if not still_alive else logging.WARNING,
            ecs_task_id=self.ecs_task_id,
            active_jobs=self._active_job_count(),
            max_concurrent_jobs=self.max_concurrent_jobs,
            consumers_still_alive=still_alive,
            drain_timeout_seconds=max(0, drain_timeout_seconds),
        )

    def run_forever(self, worker_slot: int = 1) -> None:
        log_event(
            logger,
            "SQS_CONSUMER_SLOT_STARTED",
            ecs_task_id=self.ecs_task_id,
            worker_slot=worker_slot,
            active_jobs=self._active_job_count(),
            max_concurrent_jobs=self.max_concurrent_jobs,
        )
        while not self._stop.is_set():
            try:
                log_event(
                    logger,
                    "SQS_LONG_POLL_STARTED",
                    ecs_task_id=self.ecs_task_id,
                    worker_slot=worker_slot,
                    active_jobs=self._active_job_count(),
                    max_concurrent_jobs=self.max_concurrent_jobs,
                    receive_wait_seconds=self.wait_time_seconds,
                )
                response = self.sqs.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=self.wait_time_seconds,
                    VisibilityTimeout=self.visibility_timeout,
                    AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
                )
                messages = response.get("Messages", [])
                log_event(
                    logger,
                    "SQS_LONG_POLL_COMPLETED",
                    ecs_task_id=self.ecs_task_id,
                    worker_slot=worker_slot,
                    active_jobs=self._active_job_count(),
                    max_concurrent_jobs=self.max_concurrent_jobs,
                    message_count_received=len(messages),
                )
            except Exception as exc:
                if not self._stop.is_set():
                    log_event(
                        logger,
                        "SQS_RECEIVE_FAILED",
                        level=logging.ERROR,
                        ecs_task_id=self.ecs_task_id,
                        worker_slot=worker_slot,
                        active_jobs=self._active_job_count(),
                        max_concurrent_jobs=self.max_concurrent_jobs,
                        error_type=type(exc).__name__,
                    )
                    self._stop.wait(5)
                continue
            for message in messages:
                if self._stop.is_set():
                    return
                try:
                    self.process_message(message, worker_slot=worker_slot)
                except Exception as exc:
                    # Do not let an AWS client/table exception kill this
                    # consumer thread. The un-deleted message becomes visible
                    # again when its current visibility window expires.
                    log_event(
                        logger,
                        "SQS_MESSAGE_HANDLER_CRASH",
                        level=logging.ERROR,
                        sqs_message_id=message.get("MessageId"),
                        ecs_task_id=self.ecs_task_id,
                        worker_slot=worker_slot,
                        active_jobs=self._active_job_count(),
                        max_concurrent_jobs=self.max_concurrent_jobs,
                        error_type=type(exc).__name__,
                        retry_scheduled=True,
                    )

    def process_message(self, message: dict[str, Any], *, worker_slot: int = 1) -> bool:
        """Process exactly one received SQS message; True means it was deleted."""
        message_id = message.get("MessageId", "")
        attributes = message.get("Attributes", {})
        receive_count = int(attributes.get("ApproximateReceiveCount", "1"))
        base_context = {
            "sqs_message_id": message_id,
            "sqs_receive_count": receive_count,
            "processing_attempt": receive_count,
            "ecs_task_id": self.ecs_task_id,
            "worker_slot": worker_slot,
            "max_concurrent_jobs": self.max_concurrent_jobs,
        }
        try:
            body = json.loads(message["Body"])
        except (KeyError, TypeError, json.JSONDecodeError):
            with diagnostic_context(**base_context):
                log_event(logger, "SQS_MESSAGE_INVALID_ENVELOPE", level=logging.ERROR, processing_stage="sqs_decode")
            return False
        records = body.get("Records", []) if isinstance(body, dict) else []
        if not records:
            with diagnostic_context(**base_context):
                log_event(logger, "SQS_MESSAGE_WITHOUT_RECORDS", level=logging.ERROR, processing_stage="sqs_decode")
            return False
        # S3 may batch records. Process them serially and delete the SQS message
        # only when every record is durable. Each consumer still owns one
        # message slot and never receives a replacement until this envelope ends.
        for record in records:
            bucket = record.get("s3", {}).get("bucket", {}).get("name", "")
            encoded_key = record.get("s3", {}).get("object", {}).get("key", "")
            job_id = _job_id_from_key(unquote_plus(encoded_key))
            active_jobs = self._register_active_job(worker_slot, job_id or "unknown", message_id)
            try:
                with diagnostic_context(
                    **base_context,
                    job_id=job_id or None,
                    active_jobs=active_jobs,
                ):
                    log_event(
                        logger,
                        "JOB_RECEIVED",
                        processing_stage="sqs_receive",
                        job_id=job_id or None,
                        sqs_message_id=message_id,
                        sqs_receive_count=receive_count,
                        processing_attempt=receive_count,
                        sent_timestamp=attributes.get("SentTimestamp"),
                        record_count=len(records),
                        ecs_task_id=self.ecs_task_id,
                        worker_slot=worker_slot,
                        active_jobs=active_jobs,
                        max_concurrent_jobs=self.max_concurrent_jobs,
                    )
                    if not self._process_s3_record(message, record, worker_slot=worker_slot):
                        return False
            finally:
                remaining = self._release_active_job(worker_slot, message_id)
                with diagnostic_context(**base_context, job_id=job_id or None):
                    log_event(
                        logger,
                        "JOB_SLOT_RELEASED",
                        processing_stage="job_slot_release",
                        worker_slot=worker_slot,
                        active_jobs=remaining,
                        max_concurrent_jobs=self.max_concurrent_jobs,
                    )
        with diagnostic_context(**base_context):
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
            log_event(
                logger,
                "SQS_MESSAGE_DELETED",
                processing_stage="sqs_ack",
                sqs_message_id=message_id,
                sqs_receive_count=receive_count,
                record_count=len(records),
                sqs_message_deleted=True,
                ecs_task_id=self.ecs_task_id,
                worker_slot=worker_slot,
                active_jobs=self._active_job_count(),
                max_concurrent_jobs=self.max_concurrent_jobs,
            )
        return True

    def _process_s3_record(
        self, message: dict[str, Any], record: dict[str, Any], *, worker_slot: int = 1
    ) -> bool:
        bucket = record.get("s3", {}).get("bucket", {}).get("name", "")
        encoded_key = record.get("s3", {}).get("object", {}).get("key", "")
        key = unquote_plus(encoded_key)
        job_id = _job_id_from_key(key)
        message_id = message.get("MessageId", "")
        receipt_handle = message.get("ReceiptHandle", "")
        receive_count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        if not bucket or not key or not job_id:
            log_event(
                logger,
                "S3_EVENT_INVALID_DOCUMENT_REFERENCE",
                level=logging.ERROR,
                processing_stage="s3_event_decode",
                error_category="invalid_s3_event",
            )
            return False

        log_event(
            logger,
            "JOB_CLAIM_START",
            processing_stage="job_claim",
            job_id=job_id,
            sqs_message_id=message_id,
            sqs_receive_count=receive_count,
            processing_attempt=receive_count,
            ecs_task_id=self.ecs_task_id,
        )
        try:
            job = self._acquire_job(job_id, message_id, receive_count)
        except Exception as exc:
            log_event(
                logger,
                "JOB_CLAIM_FAILED",
                level=logging.ERROR,
                processing_stage="job_claim",
                job_id=job_id,
                sqs_message_id=message_id,
                processing_attempt=receive_count,
                error_category="dynamodb_claim_error",
                error_type=type(exc).__name__,
            )
            return False
        if job is None:
            return self._handle_unacquired_job(job_id, receipt_handle)

        log_event(
            logger,
            "JOB_CLAIMED",
            processing_stage="job_claim",
            job_id=job_id,
            sqs_message_id=message_id,
            sqs_receive_count=receive_count,
            processing_attempt=receive_count,
            status=job.get("status", "PROCESSING"),
            ecs_task_id=self.ecs_task_id,
        )

        processing_owner = job.get("processing_owner")
        if not processing_owner:
            log_event(
                logger,
                "JOB_CLAIM_FAILED",
                level=logging.ERROR,
                processing_stage="job_claim",
                job_id=job_id,
                sqs_message_id=message_id,
                error_category="missing_processing_owner",
                retry_scheduled=True,
            )
            return False
        self._set_active_owner(worker_slot, message_id, processing_owner)
        log_event(
            logger,
            "JOB_OWNER_ASSIGNED",
            processing_stage="job_claim",
            job_id=job_id,
            sqs_message_id=message_id,
            processing_owner=processing_owner,
            worker_slot=worker_slot,
            active_jobs=self._active_job_count(),
            max_concurrent_jobs=self.max_concurrent_jobs,
            ecs_task_id=self.ecs_task_id,
        )
        heartbeat = VisibilityHeartbeat(
            self, receipt_handle, job_id, message_id, processing_owner, worker_slot
        )
        heartbeat.start()
        processing_started = time.monotonic()
        processing_stage = "source_event_validation"
        try:
            if self._stop.is_set():
                raise WorkerShutdown()
            # The event must still correspond to the object that JobsApi
            # created for this owner/job. The key is never treated as the
            # authorization source by itself.
            if job.get("bucket") and job["bucket"] != bucket:
                raise ValueError("El bucket del evento no coincide con el job")
            if job.get("object_key") and job["object_key"] != key:
                raise ValueError("La clave del evento no coincide con el job")
            expected_prefix = f"users/{job.get('user_id', '')}/jobs/{job_id}/"
            if job.get("user_id") and key.startswith("users/") and not key.startswith(expected_prefix):
                raise ValueError("La clave del evento no corresponde al propietario del job")
            processing_stage = "source_object_check"
            log_event(logger, "SOURCE_OBJECT_CHECK_START", processing_stage=processing_stage)
            object_info = self.s3.head_object(Bucket=bucket, Key=key)
            metadata = object_info.get("Metadata", {})
            metadata_job_id = metadata.get("job-id", job_id)
            if metadata_job_id != job_id:
                raise ValueError("El metadata job-id no coincide con la clave del documento")
            max_bytes = get_settings()["max_upload_bytes"]
            if object_info.get("ContentLength", 0) > max_bytes:
                raise ValueError("Document exceeds configured upload limit")
            processing_stage = "pdf_download"
            log_event(
                logger,
                "PDF_DOWNLOAD_START",
                processing_stage=processing_stage,
                pdf_bytes=object_info.get("ContentLength", 0),
            )
            download_started = time.monotonic()
            document = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            pdf_sha256 = hashlib.sha256(document).hexdigest()
            log_event(
                logger,
                "PDF_DOWNLOADED",
                processing_stage="pdf_download",
                pdf_bytes=len(document),
                elapsed_ms=round((time.monotonic() - download_started) * 1000),
            )
            filename = key.rsplit("/", 1)[-1]
            processing_stage = "document_processing"
            checkpoint_store = S3PageCheckpointStore(
                self.s3,
                self.results_bucket,
                job_id,
                processing_owner,
                manifest_key=job.get("checkpoint_manifest_key"),
                commit_manifest=lambda manifest_key: self._commit_checkpoint_manifest(
                    job_id, message_id, processing_owner, manifest_key
                ),
            )
            result_fingerprint = result_pipeline_fingerprint(job.get("user_prompt", ""))
            with document_context(
                job_id,
                processing_owner=processing_owner,
                worker_slot=worker_slot,
                active_jobs=self._active_job_count(),
                max_concurrent_jobs=self.max_concurrent_jobs,
            ):
                result = self.processor(
                    filename,
                    document,
                    job.get("user_prompt", ""),
                    progress_callback=lambda current, total: self._update_progress(
                        job_id, message_id, current, total, processing_owner
                    ),
                    job_id=job_id,
                    checkpoint_store=checkpoint_store,
                )
            if self._stop.is_set():
                raise WorkerShutdown()
            result_key = f"results/{job_id}.json"
            processing_stage = "result_serialize"
            log_event(logger, "RESULT_SERIALIZATION_START", processing_stage=processing_stage)
            serialization_started = time.monotonic()
            result_body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            log_event(
                logger,
                "RESULT_SERIALIZATION_COMPLETED",
                processing_stage="result_serialize",
                result_bytes=len(result_body),
                elapsed_ms=round((time.monotonic() - serialization_started) * 1000),
            )
            processing_stage = "result_s3_save"
            log_event(logger, "RESULT_S3_SAVE_START", processing_stage=processing_stage)
            self._put_result_if_absent(
                result_key,
                result_body,
                job_id,
                processing_owner,
                pdf_sha256,
                result_fingerprint,
            )
            # Do not mark complete until S3 confirms the known deterministic key.
            self.s3.head_object(Bucket=self.results_bucket, Key=result_key)
            log_event(
                logger,
                "RESULT_S3_SAVED",
                processing_stage="result_s3_save",
                s3_result_verified=True,
                result_bytes=len(result_body),
            )
            processing_stage = "job_complete_persist"
            log_event(logger, "JOB_COMPLETION_PERSIST_START", processing_stage=processing_stage)
            self._mark_completed(job_id, message_id, processing_owner, result_key)
            log_event(
                logger,
                "JOB_COMPLETED",
                processing_stage="job_complete_persist",
                job_id=job_id,
                status="COMPLETED",
                progress_percent=100,
                total_pages=result.get("pages"),
                job_status="COMPLETED",
            )
            try:
                deleted_checkpoints = checkpoint_store.delete_all()
                log_event(
                    logger,
                    "CHECKPOINT_CLEANUP",
                    processing_stage="checkpoint_cleanup",
                    job_id=job_id,
                    checkpoint_count=deleted_checkpoints,
                    outcome="deleted",
                )
            except Exception as cleanup_exc:
                # COMPLETED is already durable. SQS must still be acknowledged;
                # the ResultsBucket lifecycle is the fallback for these objects.
                log_event(
                    logger,
                    "CHECKPOINT_CLEANUP",
                    level=logging.ERROR,
                    processing_stage="checkpoint_cleanup",
                    job_id=job_id,
                    error_type=type(cleanup_exc).__name__,
                    outcome="deferred_to_lifecycle",
                )
            try:
                self.s3.delete_object(Bucket=bucket, Key=key)
                log_event(logger, "SOURCE_PDF_DELETED", processing_stage="source_cleanup", pdf_deleted=True)
            except Exception as exc:
                log_event(
                    logger,
                    "SOURCE_PDF_DELETE_FAILED",
                    level=logging.ERROR,
                    processing_stage="source_cleanup",
                    error_type=type(exc).__name__,
                )
            return True
        except Exception as exc:
            error_category = getattr(exc, "category", getattr(exc, "kind", "processing_error"))
            failure_stage = getattr(exc, "processing_stage", None) or processing_stage
            failed_page = getattr(exc, "page_number", None)
            request_id = getattr(exc, "request_id", None)
            log_event(
                logger,
                "JOB_PROCESSING_FAILED",
                level=logging.ERROR,
                processing_stage=failure_stage,
                job_id=job_id,
                sqs_message_id=message_id,
                sqs_receive_count=receive_count,
                processing_attempt=receive_count,
                page_number=failed_page,
                ollama_request_id=request_id,
                error_category=error_category,
                error_type=type(exc).__name__,
                duration_ms=round((time.monotonic() - processing_started) * 1000),
            )
            try:
                if isinstance(exc, WorkerShutdown):
                    self._release_for_retry(job_id, message_id, processing_owner, _safe_error(exc))
                    log_event(
                        logger,
                        "JOB_REQUEUED",
                        level=logging.WARNING,
                        processing_stage="task_shutdown",
                        job_id=job_id,
                        sqs_message_id=message_id,
                        sqs_receive_count=receive_count,
                        processing_attempt=receive_count,
                        page_number=failed_page,
                        status="QUEUED",
                        job_status="QUEUED",
                        error_category="worker_shutdown",
                        error_type=type(exc).__name__,
                        failure_stage=failure_stage,
                        retry_scheduled=True,
                    )
                elif receive_count >= self.max_receive_count:
                    self._mark_failed(job_id, message_id, processing_owner, _safe_error(exc))
                    log_event(
                        logger,
                        "JOB_FAILED",
                        level=logging.ERROR,
                        processing_stage="job_failed_persist",
                        job_id=job_id,
                        sqs_message_id=message_id,
                        sqs_receive_count=receive_count,
                        processing_attempt=receive_count,
                        page_number=failed_page,
                        status="FAILED",
                        job_status="FAILED",
                        error_category=error_category,
                        error_type=type(exc).__name__,
                        failure_stage=failure_stage,
                    )
                else:
                    self._release_for_retry(job_id, message_id, processing_owner, _safe_error(exc))
                    log_event(
                        logger,
                        "JOB_REQUEUED",
                        level=logging.WARNING,
                        processing_stage="job_retry_persist",
                        job_id=job_id,
                        sqs_message_id=message_id,
                        sqs_receive_count=receive_count,
                        processing_attempt=receive_count,
                        page_number=failed_page,
                        ollama_request_id=request_id,
                        status="QUEUED",
                        job_status="QUEUED",
                        error_category=error_category,
                        error_type=type(exc).__name__,
                        failure_stage=failure_stage,
                        retry_scheduled=True,
                    )
            except Exception as state_exc:
                log_event(
                    logger,
                    "JOB_FAILURE_STATE_PERSIST_FAILED",
                    level=logging.ERROR,
                    processing_stage="job_failure_persist",
                    job_id=job_id,
                    sqs_message_id=message_id,
                    processing_attempt=receive_count,
                    error_category="dynamodb_state_persist_error",
                    error_type=type(state_exc).__name__,
                    failure_stage=failure_stage,
                    retry_scheduled=True,
                )
            return False
        finally:
            heartbeat.stop()

    def _acquire_job(self, job_id: str, message_id: str, receive_count: int) -> dict | None:
        now = _now()
        now_text = _timestamp(now)
        lease_until = _timestamp(now + timedelta(seconds=self.visibility_timeout))
        processing_owner = str(uuid.uuid4())
        try:
            response = self.table.update_item(
                Key={"job_id": job_id},
                UpdateExpression=(
                    "SET #status = :processing, updated_at = :now, "
                    "started_at = if_not_exists(started_at, :now), "
                    "processing_lease_until = :lease, processing_message_id = :message, "
                    "processing_attempt = :attempt, processing_owner = :owner"
                ),
                ConditionExpression=(
                    "attribute_exists(job_id) AND ("
                    "#status IN (:upload_pending, :queued) OR "
                    "(#status = :processing AND (attribute_not_exists(processing_lease_until) "
                    "OR processing_lease_until < :now))"
                    ")"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "PROCESSING", ":upload_pending": "UPLOAD_PENDING", ":queued": "QUEUED",
                    ":now": now_text, ":lease": lease_until, ":message": message_id,
                    ":attempt": receive_count, ":owner": processing_owner,
                },
                ReturnValues="ALL_NEW",
            )
            return response.get("Attributes") or self.table.get_item(
                Key={"job_id": job_id}, ConsistentRead=True
            ).get("Item")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            return None

    def _handle_unacquired_job(self, job_id: str, receipt_handle: str) -> bool:
        item = self.table.get_item(Key={"job_id": job_id}).get("Item")
        status = (item or {}).get("status")
        if status in {"COMPLETED", "FAILED", "DELETING"} or item is None:
            log_event(
                logger,
                "STALE_SQS_EVENT_ACKNOWLEDGED",
                processing_stage="job_claim",
                job_id=job_id,
                job_status=status or "MISSING",
                outcome="delete_after_record_loop",
            )
            # The caller performs one DeleteMessage after every record in the
            # SQS envelope has been handled. Deleting here caused a duplicate
            # DeleteMessage call for terminal duplicates.
            return True
        # Another active lease owns it. Do not delete it; SQS will redeliver.
        log_event(
            logger,
            "JOB_OWNED_BY_ACTIVE_PROCESSOR",
            processing_stage="job_claim",
            job_id=job_id,
            job_status=status,
            outcome="retain_for_redelivery",
        )
        return False

    def _renew_visibility(
        self, receipt_handle: str, job_id: str, message_id: str, processing_owner: str | None = None
    ) -> None:
        started = time.monotonic()
        self.sqs.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=self.visibility_timeout,
        )
        log_event(
            logger,
            "SQS_VISIBILITY_RENEWED",
            processing_stage="visibility_heartbeat",
            job_id=job_id,
            sqs_message_id=message_id,
            visibility_timeout_seconds=self.visibility_timeout,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        now = _now()
        lease_started = time.monotonic()
        try:
            self.table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET processing_lease_until = :lease, updated_at = :now",
                ConditionExpression=(
                    "#status = :processing AND processing_message_id = :message"
                    + (" AND processing_owner = :owner" if processing_owner else "")
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "PROCESSING", ":message": message_id,
                    ":lease": _timestamp(now + timedelta(seconds=self.visibility_timeout)),
                    ":now": _timestamp(now),
                    **({":owner": processing_owner} if processing_owner else {}),
                },
            )
        except Exception as exc:
            log_event(
                logger,
                "DYNAMODB_LEASE_RENEW_FAILED",
                level=logging.ERROR,
                processing_stage="visibility_heartbeat",
                job_id=job_id,
                sqs_message_id=message_id,
                error_type=type(exc).__name__,
                lease_persisted=False,
            )
            raise
        log_event(
            logger,
            "DYNAMODB_LEASE_RENEWED",
            processing_stage="visibility_heartbeat",
            job_id=job_id,
            sqs_message_id=message_id,
            duration_ms=round((time.monotonic() - lease_started) * 1000),
            lease_persisted=True,
        )

    def _update_progress(
        self, job_id: str, message_id: str, current: int, total: int, processing_owner: str | None = None
    ) -> None:
        percent = int((current * 100) / total) if total else 0
        try:
            self.table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET total_pages = :total, current_page = :current, progress_percent = :percent, updated_at = :now",
                ConditionExpression=(
                    "#status = :processing AND processing_message_id = :message"
                    + (" AND processing_owner = :owner" if processing_owner else "")
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "PROCESSING", ":message": message_id,
                    ":total": total, ":current": current, ":percent": percent, ":now": _timestamp(),
                    **({":owner": processing_owner} if processing_owner else {}),
                },
            )
        except Exception as exc:
            log_event(
                logger,
                "PROGRESS_UPDATE_FAILED",
                level=logging.ERROR,
                processing_stage="progress_persist",
                job_id=job_id,
                sqs_message_id=message_id,
                page_number=current if current else None,
                total_pages=total,
                progress_percent=percent,
                error_type=type(exc).__name__,
            )
            raise
        log_event(
            logger,
            "PROGRESS_UPDATE_COMPLETED",
            processing_stage="progress_persist",
            job_id=job_id,
            sqs_message_id=message_id,
            page_number=current if current else None,
            total_pages=total,
            progress_percent=percent,
            status="PROCESSING",
        )

    def _commit_checkpoint_manifest(
        self, job_id: str, message_id: str, processing_owner: str, manifest_key: str
    ) -> None:
        """Make a page checkpoint recoverable only while this task owns the lease."""
        try:
            self.table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET checkpoint_manifest_key = :manifest_key, updated_at = :now",
                ConditionExpression=(
                    "#status = :processing AND processing_message_id = :message "
                    "AND processing_owner = :owner"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "PROCESSING",
                    ":message": message_id,
                    ":owner": processing_owner,
                    ":manifest_key": manifest_key,
                    ":now": _timestamp(),
                },
            )
        except Exception:
            # A DynamoDB timeout can occur after the conditional update committed.
            # Resolve that ambiguity with a strongly consistent read before we
            # report the checkpoint as recoverable to the page loop.
            current = self.table.get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item") or {}
            if (
                current.get("checkpoint_manifest_key") == manifest_key
                and current.get("processing_owner") == processing_owner
                and current.get("processing_message_id") == message_id
                and current.get("status") == "PROCESSING"
            ):
                return
            raise

    def _mark_completed(self, job_id: str, message_id: str, processing_owner: str, result_key: str) -> None:
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=(
                "SET #status = :completed, result_key = :result_key, completed_at = :now, "
                "updated_at = :now, progress_percent = :hundred "
                "REMOVE processing_lease_until, processing_message_id, processing_owner, checkpoint_manifest_key"
            ),
            ConditionExpression="#status = :processing AND processing_message_id = :message AND processing_owner = :owner",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":completed": "COMPLETED", ":processing": "PROCESSING", ":message": message_id,
                ":owner": processing_owner, ":result_key": result_key, ":now": _timestamp(), ":hundred": 100,
            },
        )

    def _release_for_retry(self, job_id: str, message_id: str, processing_owner: str, error: str) -> None:
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #status = :queued, last_error = :error, updated_at = :now REMOVE processing_lease_until, processing_message_id, processing_owner",
            ConditionExpression="#status = :processing AND processing_message_id = :message AND processing_owner = :owner",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":queued": "QUEUED", ":processing": "PROCESSING", ":message": message_id,
                ":owner": processing_owner,
                ":error": error, ":now": _timestamp(),
            },
        )

    def _mark_failed(self, job_id: str, message_id: str, processing_owner: str, error: str) -> None:
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #status = :failed, #error = :error, failed_at = :now, updated_at = :now REMOVE processing_lease_until, processing_message_id, processing_owner",
            ConditionExpression="#status = :processing AND processing_message_id = :message AND processing_owner = :owner",
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={
                ":failed": "FAILED", ":processing": "PROCESSING", ":message": message_id,
                ":owner": processing_owner,
                ":error": error, ":now": _timestamp(),
            },
        )

    def _put_result_if_absent(
        self,
        key: str,
        body: bytes,
        job_id: str,
        processing_owner: str,
        pdf_sha256: str,
        pipeline_fingerprint: str,
    ) -> None:
        """Never overwrite a result object written by another lease owner."""
        try:
            self.s3.put_object(
                Bucket=self.results_bucket,
                Key=key,
                Body=body,
                ContentType="application/json; charset=utf-8",
                Metadata={
                    "job-id": job_id,
                    "pdf-sha256": pdf_sha256,
                    "pipeline-fingerprint": pipeline_fingerprint,
                    "processing-owner": processing_owner,
                },
                IfNoneMatch="*",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code not in {"PreconditionFailed", "412", "ConditionalRequestConflict"} and status != 412:
                raise
            # A previous delivery may have persisted the deterministic result
            # and died before COMPLETED. Reuse it; never replace its bytes.
            existing = self.s3.head_object(Bucket=self.results_bucket, Key=key)
            metadata = existing.get("Metadata", {})
            expected = {
                "job-id": job_id,
                "pdf-sha256": pdf_sha256,
                "pipeline-fingerprint": pipeline_fingerprint,
            }
            if any(metadata.get(name) != value for name, value in expected.items()):
                raise RuntimeError("An existing result has a different processing identity") from None
