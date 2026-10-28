"""SQS consumer for asynchronous questions over completed document results."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.ollama_qa_client import QuestionModelError, ask_ollama
from app.qa_service import IncompatibleDocumentResult, QuestionContextTooLarge, build_question_context

logger = logging.getLogger()
logger.setLevel(logging.INFO)
_AWS_IO_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)
dynamodb = boto3.resource("dynamodb", config=_AWS_IO_CONFIG)
s3_client = boto3.client("s3", config=Config(
    connect_timeout=3,
    read_timeout=60,
    retries={"max_attempts": 2, "mode": "standard"},
))
secrets_client = boto3.client("secretsmanager", config=_AWS_IO_CONFIG)
sqs_client = boto3.client("sqs", config=_AWS_IO_CONFIG)
_cached_token: str | None = None
_cached_token_expires_at = 0.0
_TOKEN_CACHE_TTL_SECONDS = 300
_RETRYABLE_AWS_ERROR_CODES = {
    "InternalServerError",
    "ServiceUnavailable",
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "RequestThrottled",
    "RequestThrottledException",
    "TooManyRequestsException",
    "TransactionInProgressException",
}
_RETRYABLE_TRANSACTION_CANCELLATION_REASONS = {
    "TransactionConflict",
    "ProvisionedThroughputExceeded",
    "ThrottlingError",
}


class AskLeaseBusy(RuntimeError):
    pass


def _is_retryable_aws_error(exc: ClientError) -> bool:
    response = exc.response
    code = response.get("Error", {}).get("Code", "")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    if status >= 500 or status in {408, 429} or code in _RETRYABLE_AWS_ERROR_CODES:
        return True
    if code == "TransactionCanceledException":
        reasons = response.get("CancellationReasons")
        # If DynamoDB omits cancellation details, retry once through the bounded
        # SQS policy. A conditional failure after DELETE is safe to acknowledge
        # on the next delivery because the job/ask claim will no longer exist.
        if not reasons:
            return True
        return any(
            reason.get("Code") in _RETRYABLE_TRANSACTION_CANCELLATION_REASONS
            for reason in response.get("CancellationReasons", [])
            if isinstance(reason, dict)
        )
    return False


def _effective_ollama_read_timeout(lambda_context) -> int:
    configured = int(os.getenv("OLLAMA_ASK_READ_TIMEOUT", "780"))
    remaining = getattr(lambda_context, "get_remaining_time_in_millis", None)
    if not callable(remaining):
        return configured
    try:
        remaining_seconds = int(remaining()) // 1000
    except Exception as exc:
        raise QuestionModelError(
            "execution_time_unavailable",
            "No se pudo comprobar el tiempo restante de forma segura; se volverá a intentar.",
            retryable=True,
        ) from exc
    # Keep time for the HTTP connect plus conditional persistence and SQS
    # response. The template caps configured read timeout at 780s as well.
    available = remaining_seconds - 90 - 5
    if available < 1:
        raise QuestionModelError(
            "insufficient_execution_time",
            "No quedó tiempo suficiente para iniciar la consulta con seguridad; se volverá a intentar.",
            retryable=True,
        )
    return min(configured, available)


def _table(name: str):
    return dynamodb.Table(os.environ[name])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token() -> str:
    global _cached_token, _cached_token_expires_at
    now = time.monotonic()
    if _cached_token is not None and now < _cached_token_expires_at:
        return _cached_token
    secret_arn = os.getenv("OLLAMA_API_TOKEN_SECRET_ARN", "").strip()
    if not secret_arn:
        _cached_token = ""
        _cached_token_expires_at = now + _TOKEN_CACHE_TTL_SECONDS
        return _cached_token
    response = secrets_client.get_secret_value(SecretId=secret_arn)
    token = response.get("SecretString", "")
    if not isinstance(token, str) or not token.strip():
        raise QuestionModelError(
            "missing_secret",
            "La credencial del servicio de IA no está configurada.",
            retryable=False,
        )
    _cached_token = token
    _cached_token_expires_at = now + _TOKEN_CACHE_TTL_SECONDS
    return _cached_token


def _claim_ask(question_table, job_id: str, ask_id: str, processing_owner: str) -> dict | None:
    now = int(time.time())
    try:
        response = question_table.update_item(
            Key={"job_id": job_id, "ask_id": ask_id},
            UpdateExpression=(
                "SET #status = :processing, #stage = :stage, processing_owner = :owner, "
                "processing_lease_until = :lease, updated_at = :updated, "
                "processing_attempt = if_not_exists(processing_attempt, :zero) + :one"
            ),
            ConditionExpression=(
                "attribute_exists(ask_id) AND expires_at > :now AND "
                "(#status = :pending OR (#status = :processing AND processing_lease_until < :now))"
            ),
            ExpressionAttributeNames={"#status": "status", "#stage": "processing_stage"},
            ExpressionAttributeValues={
                ":processing": "PROCESSING",
                ":stage": "LOADING_CONTEXT",
                ":pending": "PENDING",
                ":owner": processing_owner,
                ":lease": now + int(os.getenv("ASK_PROCESSING_LEASE_SECONDS", "960")),
                ":now": now,
                ":updated": _now_iso(),
                ":zero": 0,
                ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        current = question_table.get_item(
            Key={"job_id": job_id, "ask_id": ask_id}, ConsistentRead=True
        ).get("Item")
        if not current or current.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
            return None
        if current.get("status") == "PROCESSING":
            raise AskLeaseBusy("Another invocation owns this question") from None
        return None


def _set_processing_stage(question_table, job_id: str, ask_id: str, owner: str, stage: str) -> bool:
    """Update a visible stage only while this invocation still owns a live lease."""
    try:
        question_table.update_item(
            Key={"job_id": job_id, "ask_id": ask_id},
            UpdateExpression="SET #stage = :stage, updated_at = :updated",
            ConditionExpression=(
                "#status = :processing AND processing_owner = :owner "
                "AND processing_lease_until > :now AND attribute_exists(ask_id)"
            ),
            ExpressionAttributeNames={"#status": "status", "#stage": "processing_stage"},
            ExpressionAttributeValues={
                ":stage": stage, ":updated": _now_iso(), ":processing": "PROCESSING",
                ":owner": owner, ":now": int(time.time()),
            },
        )
        logger.info("ASK_STAGE_CHANGED job_id=%s ask_id=%s processing_owner=%s processing_stage=%s",
                    job_id, ask_id, owner, stage)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("ASK_STAGE_UPDATE_SKIPPED job_id=%s ask_id=%s processing_owner=%s processing_stage=%s reason=lease_not_owned",
                        job_id, ask_id, owner, stage)
            return False
        raise


def _load_completed_job(job_id: str, user_id: str) -> dict | None:
    item = _table("JOBS_TABLE").get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item")
    if not item or item.get("user_id") != user_id or item.get("status") != "COMPLETED":
        return None
    result_key = item.get("result_key")
    if result_key != f"results/{job_id}.json":
        return None
    return item


def _load_result(bucket: str, key: str) -> dict:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    result = json.loads(body)
    if not isinstance(result, dict):
        raise IncompatibleDocumentResult("Stored result is not a JSON object")
    return result


def _complete_ask(question_table, job_id: str, ask_id: str, user_id: str, owner: str,
                  answer: dict, context_limited: bool) -> bool:
    now = _now_iso()
    model_set = ", #model = :model" if isinstance(answer.get("model"), str) and answer["model"] else ""
    values = {
        ":completed": "COMPLETED", ":processing": "PROCESSING", ":worker": owner,
        ":answer": answer["answer"], ":pages": answer["source_pages"], ":limited": context_limited,
        ":stage": "COMPLETED", ":now": now,
        ":context_mode": answer.get("context_mode", "FULL_TRANSCRIPT"),
    }
    expression_names = {"#status": "status", "#stage": "processing_stage"}
    if model_set:
        values[":model"] = answer["model"]
        expression_names["#model"] = "model"
    try:
        dynamodb.meta.client.transact_write_items(TransactItems=[
            {
                "ConditionCheck": {
                    "TableName": os.environ["JOBS_TABLE"],
                    "Key": {"job_id": job_id},
                    "ConditionExpression": "user_id = :owner AND #status = :completed",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {":owner": user_id, ":completed": "COMPLETED"},
                }
            },
            {
                "Update": {
                    "TableName": question_table.name,
                    "Key": {"job_id": job_id, "ask_id": ask_id},
                    "UpdateExpression": (
                        "SET #status = :completed, answer = :answer, source_pages = :pages, "
                        "context_limited = :limited, context_mode = :context_mode"
                        + model_set
                        + ", #stage = :stage, completed_at = :now, updated_at = :now "
                        "REMOVE processing_owner, processing_lease_until"
                    ),
                    "ConditionExpression": "#status = :processing AND processing_owner = :worker AND attribute_exists(ask_id)",
                    "ExpressionAttributeNames": expression_names,
                    "ExpressionAttributeValues": values,
                }
            },
        ])
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            if _is_retryable_aws_error(exc):
                raise
            logger.info("ASK_COMPLETION_SUPPRESSED job_id=%s ask_id=%s reason=job_changed_or_deleted", job_id, ask_id)
            return False
        raise


def _set_terminal_error(question_table, job_id: str, ask_id: str, owner: str, message: str,
                        error_category: str = "processing_failed") -> None:
    try:
        question_table.update_item(
            Key={"job_id": job_id, "ask_id": ask_id},
            UpdateExpression=(
                "SET #status = :failed, #stage = :stage, #error = :error, error_category = :category, "
                "failed_at = :now, updated_at = :now "
                "REMOVE processing_owner, processing_lease_until"
            ),
            ConditionExpression="#status = :processing AND processing_owner = :owner AND attribute_exists(ask_id)",
            ExpressionAttributeNames={"#status": "status", "#stage": "processing_stage", "#error": "error"},
            ExpressionAttributeValues={
                ":failed": "FAILED", ":processing": "PROCESSING", ":owner": owner,
                ":stage": "FAILED",
                ":error": message, ":now": _now_iso(),
                ":category": error_category,
            },
        )
        logger.error("ASK_FAILED job_id=%s ask_id=%s processing_owner=%s error_category=%s",
                     job_id, ask_id, owner, error_category)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def _release_for_retry(question_table, job_id: str, ask_id: str, owner: str) -> bool:
    try:
        question_table.update_item(
            Key={"job_id": job_id, "ask_id": ask_id},
            UpdateExpression="SET #status = :pending, #stage = :stage, updated_at = :now REMOVE processing_owner, processing_lease_until",
            ConditionExpression="#status = :processing AND processing_owner = :owner AND attribute_exists(ask_id)",
            ExpressionAttributeNames={"#status": "status", "#stage": "processing_stage"},
            ExpressionAttributeValues={
                ":pending": "PENDING", ":stage": "RETRYING", ":processing": "PROCESSING", ":owner": owner, ":now": _now_iso(),
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return False


def _change_visibility(record: dict, visibility_seconds: int, job_id: str, ask_id: str) -> None:
    """Shorten handled retries; queue-level visibility stays at 5400 for Lambda safety."""
    receipt_handle = record.get("receiptHandle")
    queue_url = os.getenv("QUESTION_QUEUE_URL", "").strip()
    if not receipt_handle or not queue_url:
        logger.warning("ASK_VISIBILITY_NOT_CHANGED job_id=%s ask_id=%s reason=missing_sqs_metadata", job_id, ask_id)
        return
    try:
        sqs_client.change_message_visibility(
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=visibility_seconds,
        )
    except Exception as exc:
        # Partial batch failure still preserves the queue's configured visibility.
        logger.warning("ASK_VISIBILITY_CHANGE_FAILED job_id=%s ask_id=%s error_category=%s",
                       job_id, ask_id, type(exc).__name__)


def _retry_delay(receive_count: int, error: QuestionModelError | None = None) -> int:
    if error and error.category in {
        "coordinator_busy", "coordinator_unavailable", "coordinator_shutdown", "coordinator_request_timeout",
    }:
        configured = int(os.getenv("OLLAMA_COORDINATOR_RETRY_DELAY_SECONDS", "900"))
        suggested = error.retry_after_seconds or 0
        return min(5400, max(1, configured, suggested))
    base = max(1, int(os.getenv("QUESTION_RETRY_DELAY_SECONDS", "30")))
    return min(base * max(1, receive_count), 300)


def _process_dlq_message(record: dict) -> bool:
    """Make exhausted or abruptly terminated asks visibly terminal after DLQ delivery."""
    message_id = record.get("messageId", "unknown")
    try:
        payload = json.loads(record.get("body") or "{}")
    except (TypeError, ValueError):
        logger.error("ASK_DLQ message_id=%s outcome=invalid_message", message_id)
        return True
    if not isinstance(payload, dict):
        logger.error("ASK_DLQ message_id=%s outcome=invalid_message", message_id)
        return True
    job_id, ask_id, user_id = (payload.get(field) for field in ("job_id", "ask_id", "user_id"))
    if not all(isinstance(value, str) and value for value in (job_id, ask_id, user_id)):
        logger.error("ASK_DLQ message_id=%s outcome=invalid_message", message_id)
        return True

    try:
        table = _table("QUESTION_TABLE")
        now = int(time.time())
        current = table.get_item(
            Key={"job_id": job_id, "ask_id": ask_id}, ConsistentRead=True
        ).get("Item")
        if not current or current.get("user_id") != user_id or current.get("expires_at", 0) <= now:
            logger.info("ASK_DLQ job_id=%s ask_id=%s message_id=%s outcome=missing_or_expired", job_id, ask_id, message_id)
            return True
        if current.get("status") not in {"PENDING", "PROCESSING"}:
            logger.info("ASK_DLQ job_id=%s ask_id=%s message_id=%s outcome=already_terminal", job_id, ask_id, message_id)
            return True
        if current.get("status") == "PROCESSING" and int(current.get("processing_lease_until", 0)) > now:
            # Standard SQS delivery is at least once. A duplicate can reach the
            # DLQ while the original Lambda still owns a valid lease.
            receipt_handle = record.get("receiptHandle")
            queue_url = os.getenv("QUESTION_DLQ_URL", "").strip()
            if receipt_handle and queue_url:
                try:
                    sqs_client.change_message_visibility(
                        QueueUrl=queue_url, ReceiptHandle=receipt_handle, VisibilityTimeout=60,
                    )
                except Exception as exc:
                    logger.warning("ASK_DLQ_VISIBILITY_CHANGE_FAILED job_id=%s ask_id=%s error_category=%s",
                                   job_id, ask_id, type(exc).__name__)
            logger.info("ASK_DLQ job_id=%s ask_id=%s message_id=%s outcome=active_lease_deferred", job_id, ask_id, message_id)
            return False
        table.update_item(
            Key={"job_id": job_id, "ask_id": ask_id},
            UpdateExpression=(
                "SET #status = :failed, #stage = :stage, #error = :error, error_category = :category, "
                "failed_at = :now_iso, updated_at = :now_iso "
                "REMOVE processing_owner, processing_lease_until"
            ),
            ConditionExpression=(
                "attribute_exists(ask_id) AND user_id = :owner AND expires_at > :now AND "
                "(#status = :pending OR (#status = :processing AND "
                "(attribute_not_exists(processing_lease_until) OR processing_lease_until <= :now)))"
            ),
            ExpressionAttributeNames={"#status": "status", "#stage": "processing_stage", "#error": "error"},
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":stage": "FAILED",
                ":error": "No se pudo completar la consulta después de los reintentos permitidos.",
                ":category": "retry_exhausted",
                ":now_iso": _now_iso(),
                ":owner": user_id,
                ":now": now,
                ":pending": "PENDING",
                ":processing": "PROCESSING",
            },
        )
        logger.error("ASK_DLQ job_id=%s ask_id=%s message_id=%s outcome=marked_failed", job_id, ask_id, message_id)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("ASK_DLQ job_id=%s ask_id=%s message_id=%s outcome=already_terminal_or_deleted",
                        job_id, ask_id, message_id)
            return True
        logger.error("ASK_DLQ job_id=%s ask_id=%s message_id=%s outcome=retry error_category=%s",
                     job_id, ask_id, message_id, exc.response.get("Error", {}).get("Code", "ClientError"))
        return False
    except Exception as exc:
        logger.error("ASK_DLQ job_id=%s ask_id=%s message_id=%s outcome=retry error_category=%s",
                     job_id, ask_id, message_id, type(exc).__name__)
        return False


def _process_message(record: dict, lambda_context=None) -> bool:
    """Return True when this delivery can be acknowledged by the event source mapping."""
    message_id = record.get("messageId", "unknown")
    try:
        payload = json.loads(record.get("body") or "{}")
    except (TypeError, ValueError):
        logger.error("ASK_MESSAGE_INVALID message_id=%s", message_id)
        return True
    if not isinstance(payload, dict):
        logger.error("ASK_MESSAGE_INVALID message_id=%s", message_id)
        return True
    job_id = payload.get("job_id")
    ask_id = payload.get("ask_id")
    user_id = payload.get("user_id")
    if not all(isinstance(value, str) and value for value in (job_id, ask_id, user_id)):
        logger.error("ASK_MESSAGE_INVALID message_id=%s", message_id)
        return True

    receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
    logger.info("ASK_MESSAGE_RECEIVED job_id=%s ask_id=%s message_id=%s processing_attempt=%d",
                job_id, ask_id, message_id, receive_count)
    question_table = _table("QUESTION_TABLE")
    processing_owner = str(uuid.uuid4())
    try:
        ask = _claim_ask(question_table, job_id, ask_id, processing_owner)
        if ask is None:
            logger.info("ASK_DELIVERY_SKIPPED job_id=%s ask_id=%s message_id=%s", job_id, ask_id, message_id)
            return True
        processing_attempt = int(ask.get("processing_attempt", receive_count))
        logger.info("ASK_CLAIMED job_id=%s ask_id=%s message_id=%s processing_attempt=%d processing_owner=%s",
                    job_id, ask_id, message_id, processing_attempt, processing_owner)
    except AskLeaseBusy:
        logger.info("ASK_DUPLICATE_DEFERRED job_id=%s ask_id=%s message_id=%s", job_id, ask_id, message_id)
        return False
    except Exception as exc:
        logger.error("ASK_CLAIM_FAILED job_id=%s ask_id=%s error_category=%s", job_id, ask_id, type(exc).__name__)
        return False

    processing_started = time.monotonic()
    if ask.get("user_id") != user_id:
        _set_terminal_error(question_table, job_id, ask_id, processing_owner, "La consulta no está disponible.")
        logger.error("ASK_OWNER_MISMATCH job_id=%s ask_id=%s", job_id, ask_id)
        return True

    job = _load_completed_job(job_id, user_id)
    if job is None:
        _set_terminal_error(question_table, job_id, ask_id, processing_owner, "El documento ya no está disponible.")
        logger.info("ASK_DOCUMENT_UNAVAILABLE job_id=%s ask_id=%s", job_id, ask_id)
        return True

    try:
        result = _load_result(os.environ["RESULTS_BUCKET"], job["result_key"])
        question = ask.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise IncompatibleDocumentResult("Question record is not valid")
        context = build_question_context(
            result,
            question,
            num_ctx=int(os.getenv("OLLAMA_ASK_NUM_CTX", "8192")),
            num_predict=int(os.getenv("OLLAMA_ASK_NUM_PREDICT", "2000")),
            evidence_num_predict=int(os.getenv("OLLAMA_ASK_EVIDENCE_NUM_PREDICT", "300")),
        )
        logger.info(
            "ASK_CONTEXT_LOADED job_id=%s ask_id=%s processing_attempt=%d page_count=%d context_chars=%d estimated_input_tokens=%d context_mode=%s section_count=%d",
            job_id, ask_id, processing_attempt, len(context.page_numbers), len(context.text),
            context.estimated_input_tokens, context.context_mode, context.source_chunk_count,
        )
        if not _set_processing_stage(question_table, job_id, ask_id, processing_owner, "WAITING_MODEL"):
            return True
        effective_read_timeout = _effective_ollama_read_timeout(lambda_context)
        request_id = str(uuid.uuid4())
        ollama_started = time.monotonic()
        logger.info(
            "ASK_OLLAMA_REQUEST_STARTED job_id=%s ask_id=%s processing_attempt=%d processing_owner=%s request_id=%s model=%s num_ctx=%s num_predict=%s read_timeout=%d context_mode=%s estimated_input_tokens=%d",
            job_id, ask_id, processing_attempt, processing_owner, request_id,
            os.getenv("OLLAMA_ASK_MODEL", "qwen3-vl:8b-instruct"), os.getenv("OLLAMA_ASK_NUM_CTX", "8192"),
            os.getenv("OLLAMA_ASK_NUM_PREDICT", "2000"), effective_read_timeout,
            context.context_mode, context.estimated_input_tokens,
        )
        try:
            answer = ask_ollama(
                question, context, _token(), effective_read_timeout,
                lambda_context=lambda_context, request_id=request_id,
            )
        except QuestionModelError as exc:
            logger.warning(
                "ASK_OLLAMA_REQUEST_FAILED job_id=%s ask_id=%s processing_attempt=%d request_id=%s duration_ms=%d error_category=%s http_status=%s",
                job_id, ask_id, processing_attempt, request_id,
                round((time.monotonic() - ollama_started) * 1000), exc.category, exc.http_status,
            )
            raise
        logger.info("ASK_OLLAMA_REQUEST_COMPLETED job_id=%s ask_id=%s processing_attempt=%d request_id=%s duration_ms=%d",
                    job_id, ask_id, processing_attempt, request_id,
                    round((time.monotonic() - ollama_started) * 1000))
        if not _set_processing_stage(question_table, job_id, ask_id, processing_owner, "SAVING_RESULT"):
            return True
        completed = _complete_ask(
            question_table, job_id, ask_id, user_id, processing_owner, answer, context.context_limited
        )
        if completed:
            logger.info(
                "ASK_COMPLETED job_id=%s ask_id=%s message_id=%s processing_attempt=%d processing_owner=%s duration_ms=%d source_pages=%d context_limited=%s",
                job_id, ask_id, message_id, processing_attempt, processing_owner,
                round((time.monotonic() - processing_started) * 1000), len(answer["source_pages"]), context.context_limited,
            )
        return True
    except QuestionModelError as exc:
        logger.warning(
            "ASK_PROCESSING_FAILED job_id=%s ask_id=%s message_id=%s receive_count=%d error_category=%s retryable=%s",
            job_id, ask_id, message_id, receive_count, exc.category, exc.retryable,
        )
        if exc.retryable and receive_count < int(os.getenv("SQS_MAX_RECEIVE_COUNT", "3")):
            _release_for_retry(question_table, job_id, ask_id, processing_owner)
            retry_delay = _retry_delay(receive_count, exc)
            _change_visibility(record, retry_delay, job_id, ask_id)
            logger.info("ASK_RETRY_SCHEDULED job_id=%s ask_id=%s processing_attempt=%d delay_seconds=%d error_category=%s",
                        job_id, ask_id, processing_attempt, retry_delay, exc.category)
            return False
        _set_terminal_error(question_table, job_id, ask_id, processing_owner, exc.public_message, exc.category)
        if exc.retryable:
            _change_visibility(record, 0, job_id, ask_id)
        return bool(not exc.retryable)
    except QuestionContextTooLarge:
        context_value = locals().get("context")
        logger.warning(
            "ASK_CONTEXT_TOO_LARGE job_id=%s ask_id=%s estimated_input_tokens=%s num_ctx=%s",
            job_id, ask_id, getattr(context_value, "estimated_input_tokens", None),
            os.getenv("OLLAMA_ASK_NUM_CTX", "8192"),
        )
        _set_terminal_error(
            question_table, job_id, ask_id, processing_owner,
            "La transcripción completa excede el límite seguro de contexto y no se envió a la IA.",
            "context_too_large",
        )
        return True
    except (IncompatibleDocumentResult, ValueError, json.JSONDecodeError) as exc:
        logger.warning("ASK_RESULT_INVALID job_id=%s ask_id=%s error_category=%s", job_id, ask_id, type(exc).__name__)
        _set_terminal_error(question_table, job_id, ask_id, processing_owner, "No se pudo leer el resultado de este documento.")
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        retryable = _is_retryable_aws_error(exc)
        logger.warning(
            "ASK_AWS_OPERATION_FAILED job_id=%s ask_id=%s message_id=%s receive_count=%d error_category=%s retryable=%s",
            job_id, ask_id, message_id, receive_count, code, retryable,
        )
        if retryable and receive_count < int(os.getenv("SQS_MAX_RECEIVE_COUNT", "3")):
            _release_for_retry(question_table, job_id, ask_id, processing_owner)
            retry_delay = _retry_delay(receive_count)
            _change_visibility(record, retry_delay, job_id, ask_id)
            logger.info("ASK_RETRY_SCHEDULED job_id=%s ask_id=%s processing_attempt=%d delay_seconds=%d error_category=%s",
                        job_id, ask_id, processing_attempt, retry_delay, code)
            return False
        _set_terminal_error(question_table, job_id, ask_id, processing_owner, "No se pudo completar la consulta del documento.")
        return not retryable
    except Exception as exc:
        logger.error(
            "ASK_UNEXPECTED_FAILURE job_id=%s ask_id=%s message_id=%s receive_count=%d error_category=%s",
            job_id, ask_id, message_id, receive_count, type(exc).__name__,
        )
        if receive_count < int(os.getenv("SQS_MAX_RECEIVE_COUNT", "3")):
            _release_for_retry(question_table, job_id, ask_id, processing_owner)
            retry_delay = _retry_delay(receive_count)
            _change_visibility(record, retry_delay, job_id, ask_id)
            logger.info("ASK_RETRY_SCHEDULED job_id=%s ask_id=%s processing_attempt=%d delay_seconds=%d error_category=%s",
                        job_id, ask_id, processing_attempt, retry_delay, type(exc).__name__)
            return False
        _set_terminal_error(question_table, job_id, ask_id, processing_owner, "No se pudo completar la consulta del documento.")
        _change_visibility(record, 0, job_id, ask_id)
        return False


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        if not _process_message(record, context):
            failures.append({"itemIdentifier": record.get("messageId", "")})
    return {"batchItemFailures": failures}


def lambda_dlq_handler(event, context):
    """Reconcile asks moved to the DLQ, including hard Lambda timeouts/crashes."""
    del context
    failures = []
    for record in event.get("Records", []):
        if not _process_dlq_message(record):
            failures.append({"itemIdentifier": record.get("messageId", "")})
    return {"batchItemFailures": failures}
