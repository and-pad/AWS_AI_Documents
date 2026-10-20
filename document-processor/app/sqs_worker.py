"""Long-poll SQS consumer run inside the Fargate document-processor task.

The queue remains the durable unit of work.  A DynamoDB lease protects against
at-least-once delivery during ECS deployments while SQS visibility is renewed
independently of a long Ollama call.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

from app.document_service import process_pdf
from app.settings import get_settings
from app.diagnostics import document_context

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
    return "El procesamiento falló; se reintentará"


class VisibilityHeartbeat:
    """Keeps one received message invisible while a synchronous VLM call runs."""

    def __init__(self, worker: "SqsDocumentWorker", receipt_handle: str, job_id: str, message_id: str):
        self.worker = worker
        self.receipt_handle = receipt_handle
        self.job_id = job_id
        self.message_id = message_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"sqs-visibility-{job_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.worker.visibility_heartbeat_seconds):
            try:
                self.worker._renew_visibility(self.receipt_handle, self.job_id, self.message_id)
            except Exception:
                # A failed renewal must not delete the message. SQS redelivery
                # plus the DynamoDB lease is the recovery path.
                logger.exception("No se pudo renovar la visibilidad SQS job_id=%s", self.job_id)


class SqsDocumentWorker:
    """A single-threaded consumer. One ECS task processes one document at once."""

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
        # Compose/local API mode has no queue configuration. Delay SDK client
        # construction in that mode so starting `/health` never probes AWS
        # credentials or metadata services.
        self.sqs = sqs_client
        self.s3 = s3_client
        self.table = table
        if self.enabled:
            self.sqs = self.sqs or boto3.client("sqs")
            self.s3 = self.s3 or boto3.client("s3")
            self.table = self.table or boto3.resource("dynamodb").Table(self.jobs_table_name)
        self.processor = processor
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: tuple[str, str, str] | None = None  # job id, receipt, message id
        self._current_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.queue_url and self.documents_bucket and self.results_bucket and self.jobs_table_name)

    def start(self) -> None:
        if not self.enabled:
            logger.info("SQS worker disabled: queue/bucket/table configuration is incomplete")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="sqs-document-worker", daemon=True)
        self._thread.start()
        logger.info("SQS document worker started")

    def stop(self) -> None:
        """Stop receiving. The in-flight message is deliberately retained."""
        self._stop.set()
        with self._current_lock:
            current = self._current
        if current:
            job_id, _receipt, message_id = current
            try:
                self._release_for_retry(job_id, message_id, "El procesamiento se interrumpió; se reintentará")
            except Exception:
                logger.exception("No se pudo liberar el job durante shutdown job_id=%s", job_id)
        if self._thread:
            self._thread.join(timeout=5)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                response = self.sqs.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=self.wait_time_seconds,
                    VisibilityTimeout=self.visibility_timeout,
                    AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
                )
            except Exception:
                if not self._stop.is_set():
                    logger.exception("Error durante long polling de DocumentQueue")
                    self._stop.wait(5)
                continue
            for message in response.get("Messages", []):
                if self._stop.is_set():
                    return
                self.process_message(message)

    def process_message(self, message: dict[str, Any]) -> bool:
        """Process exactly one received SQS message; True means it was deleted."""
        try:
            body = json.loads(message["Body"])
        except (KeyError, TypeError, json.JSONDecodeError):
            logger.error("SQS message has invalid S3 event envelope; leaving it for DLQ")
            return False
        records = body.get("Records", []) if isinstance(body, dict) else []
        if not records:
            logger.error("SQS message has no S3 records; leaving it for DLQ")
            return False
        # S3 may batch records. Process them serially and delete the SQS message
        # only when every record is durable, preserving one-document execution.
        for record in records:
            if not self._process_s3_record(message, record):
                return False
        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
        return True

    def _process_s3_record(self, message: dict[str, Any], record: dict[str, Any]) -> bool:
        bucket = record.get("s3", {}).get("bucket", {}).get("name", "")
        encoded_key = record.get("s3", {}).get("object", {}).get("key", "")
        key = unquote_plus(encoded_key)
        job_id = _job_id_from_key(key)
        message_id = message.get("MessageId", "")
        receipt_handle = message.get("ReceiptHandle", "")
        receive_count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        if not bucket or not key or not job_id:
            logger.error("S3 event does not contain a supported document key; leaving message for DLQ")
            return False

        job = self._acquire_job(job_id, message_id, receive_count)
        if job is None:
            return self._handle_unacquired_job(job_id, receipt_handle)

        heartbeat = VisibilityHeartbeat(self, receipt_handle, job_id, message_id)
        with self._current_lock:
            self._current = (job_id, receipt_handle, message_id)
        heartbeat.start()
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
            object_info = self.s3.head_object(Bucket=bucket, Key=key)
            metadata = object_info.get("Metadata", {})
            metadata_job_id = metadata.get("job-id", job_id)
            if metadata_job_id != job_id:
                raise ValueError("El metadata job-id no coincide con la clave del documento")
            max_bytes = get_settings()["max_upload_bytes"]
            if object_info.get("ContentLength", 0) > max_bytes:
                raise ValueError("Document exceeds configured upload limit")
            document = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            filename = key.rsplit("/", 1)[-1]
            with document_context(job_id):
                result = self.processor(
                    filename,
                    document,
                    job.get("user_prompt", ""),
                    progress_callback=lambda current, total: self._update_progress(job_id, message_id, current, total),
                )
            if self._stop.is_set():
                raise WorkerShutdown()
            result_key = f"results/{job_id}.json"
            self.s3.put_object(
                Bucket=self.results_bucket,
                Key=result_key,
                Body=json.dumps(result, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json; charset=utf-8",
            )
            # Do not mark complete until S3 confirms the known deterministic key.
            self.s3.head_object(Bucket=self.results_bucket, Key=result_key)
            self._mark_completed(job_id, message_id, result_key)
            try:
                self.s3.delete_object(Bucket=bucket, Key=key)
            except Exception:
                logger.exception("Could not remove completed source PDF job_id=%s", job_id)
            logger.info("Document completed job_id=%s attempt=%d", job_id, receive_count)
            return True
        except Exception as exc:
            if receive_count >= self.max_receive_count:
                self._mark_failed(job_id, message_id)
            else:
                self._release_for_retry(job_id, message_id, _safe_error(exc))
            logger.exception("Document processing failed job_id=%s attempt=%d", job_id, receive_count)
            return False
        finally:
            heartbeat.stop()
            with self._current_lock:
                self._current = None

    def _acquire_job(self, job_id: str, message_id: str, receive_count: int) -> dict | None:
        now = _now()
        now_text = _timestamp(now)
        lease_until = _timestamp(now + timedelta(seconds=self.visibility_timeout))
        try:
            response = self.table.update_item(
                Key={"job_id": job_id},
                UpdateExpression=(
                    "SET #status = :processing, updated_at = :now, "
                    "started_at = if_not_exists(started_at, :now), "
                    "processing_lease_until = :lease, processing_message_id = :message, "
                    "processing_attempt = :attempt"
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
                    ":now": now_text, ":lease": lease_until, ":message": message_id, ":attempt": receive_count,
                },
                ReturnValues="ALL_NEW",
            )
            return response.get("Attributes") or self.table.get_item(Key={"job_id": job_id}).get("Item")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            return None

    def _handle_unacquired_job(self, job_id: str, receipt_handle: str) -> bool:
        item = self.table.get_item(Key={"job_id": job_id}).get("Item")
        status = (item or {}).get("status")
        if status in {"COMPLETED", "FAILED", "DELETING"} or item is None:
            logger.info("Deleting stale SQS event job_id=%s status=%s", job_id, status)
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)
            return True
        # Another active lease owns it. Do not delete it; SQS will redeliver.
        logger.info("Job is owned by an active processor job_id=%s status=%s", job_id, status)
        return False

    def _renew_visibility(self, receipt_handle: str, job_id: str, message_id: str) -> None:
        self.sqs.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=self.visibility_timeout,
        )
        now = _now()
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET processing_lease_until = :lease, updated_at = :now",
            ConditionExpression="#status = :processing AND processing_message_id = :message",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":processing": "PROCESSING", ":message": message_id,
                ":lease": _timestamp(now + timedelta(seconds=self.visibility_timeout)),
                ":now": _timestamp(now),
            },
        )

    def _update_progress(self, job_id: str, message_id: str, current: int, total: int) -> None:
        percent = int((current * 100) / total) if total else 0
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET total_pages = :total, current_page = :current, progress_percent = :percent, updated_at = :now",
            ConditionExpression="#status = :processing AND processing_message_id = :message",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":processing": "PROCESSING", ":message": message_id,
                ":total": total, ":current": current, ":percent": percent, ":now": _timestamp(),
            },
        )

    def _mark_completed(self, job_id: str, message_id: str, result_key: str) -> None:
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=(
                "SET #status = :completed, result_key = :result_key, completed_at = :now, "
                "updated_at = :now, progress_percent = :hundred "
                "REMOVE processing_lease_until, processing_message_id"
            ),
            ConditionExpression="#status = :processing AND processing_message_id = :message",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":completed": "COMPLETED", ":processing": "PROCESSING", ":message": message_id,
                ":result_key": result_key, ":now": _timestamp(), ":hundred": 100,
            },
        )

    def _release_for_retry(self, job_id: str, message_id: str, error: str) -> None:
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #status = :queued, last_error = :error, updated_at = :now REMOVE processing_lease_until, processing_message_id",
            ConditionExpression="#status = :processing AND processing_message_id = :message",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":queued": "QUEUED", ":processing": "PROCESSING", ":message": message_id,
                ":error": error, ":now": _timestamp(),
            },
        )

    def _mark_failed(self, job_id: str, message_id: str) -> None:
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #status = :failed, #error = :error, failed_at = :now, updated_at = :now REMOVE processing_lease_until, processing_message_id",
            ConditionExpression="#status = :processing AND processing_message_id = :message",
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={
                ":failed": "FAILED", ":processing": "PROCESSING", ":message": message_id,
                ":error": "No se pudo procesar el documento", ":now": _timestamp(),
            },
        )
