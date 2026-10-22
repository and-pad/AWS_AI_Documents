import json
import logging
import os
import re
import time
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import PurePath

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
ddb = boto3.resource("dynamodb")


def _create_s3_client():
    # AWS_REGION is set by Lambda; AWS_DEFAULT_REGION supports local runtimes.
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    local_endpoint = os.getenv("AWS_ENDPOINT_URL_S3") or os.getenv("AWS_ENDPOINT_URL")
    endpoint_url = local_endpoint
    options = {"region_name": region}
    if endpoint_url is None and region:
        # Explicit endpoint avoids legacy global S3 hosts in presigned URLs.
        endpoint_url = f"https://s3.{region}.amazonaws.com"
        options["config"] = Config(s3={"addressing_style": "virtual"})
    if endpoint_url:
        options["endpoint_url"] = endpoint_url
    return boto3.client("s3", **options)


s3 = _create_s3_client()
sqs = boto3.client("sqs")
USER_JOBS_PAGE_SIZE = 100
MAX_USER_JOBS = int(os.getenv("MAX_USER_JOBS", "20"))
MAX_USER_QUESTIONS_PER_DAY = int(os.getenv("MAX_USER_QUESTIONS_PER_DAY", "50"))
MAX_QUESTION_LENGTH = 2000
QUESTION_RECORD_TTL_SECONDS = 86400
QUESTION_STALE_AFTER_SECONDS = int(os.getenv("QUESTION_STALE_AFTER_SECONDS", "32400"))
PRESIGNED_URL_TTL_SECONDS = 900
PRESIGNED_URL_DELETE_GRACE_SECONDS = 300
QUOTA_JOB_ID_PREFIX = "__user_quota__#"
PUBLIC_JOB_FIELDS = (
    "job_id",
    "status",
    "filename",
    "created_at",
    "updated_at",
    "uploaded_at",
    "started_at",
    "completed_at",
    "failed_at",
    "total_pages",
    "current_page",
    "progress_percent",
    "error",
    "last_error",
)


def _table():
    return ddb.Table(os.environ["JOBS_TABLE"])


def _question_table():
    return ddb.Table(os.environ["QUESTION_TABLE"])


def _quota_key(user_id: str) -> str:
    return f"{QUOTA_JOB_ID_PREFIX}{user_id}"


def _query_user_job_count(table, user_id: str) -> int:
    count = 0
    query = {
        "IndexName": "UserJobsIndex",
        "KeyConditionExpression": Key("user_id").eq(user_id),
        "Select": "COUNT",
    }
    while True:
        result = table.query(**query)
        count += result.get("Count", 0)
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            return count
        query["ExclusiveStartKey"] = last_key


def _ensure_user_quota(table, user_id: str) -> dict:
    key = {"job_id": _quota_key(user_id)}
    result = table.get_item(Key=key, ConsistentRead=True)
    if result.get("Item"):
        return result["Item"]

    # Seed once for users who already had jobs before quota accounting existed.
    initial_count = _query_user_job_count(table, user_id)
    try:
        table.put_item(
            Item={
                "job_id": key["job_id"],
                "record_type": "USER_QUOTA",
                "active_count": initial_count,
            },
            ConditionExpression="attribute_not_exists(job_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    return table.get_item(Key=key, ConsistentRead=True)["Item"]


def _transact_write_items(transact_items: list[dict]) -> dict:
    return ddb.meta.client.transact_write_items(TransactItems=transact_items)


def _transact_create_job(table, item: dict, user_id: str) -> None:
    _transact_write_items([
        {
            "Update": {
                "TableName": table.name,
                "Key": {"job_id": _quota_key(user_id)},
                "UpdateExpression": "ADD #count :one",
                "ConditionExpression": "attribute_exists(#count) AND #count < :limit",
                "ExpressionAttributeNames": {"#count": "active_count"},
                "ExpressionAttributeValues": {":one": 1, ":limit": MAX_USER_JOBS},
            }
        },
        {
            "Put": {
                "TableName": table.name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(job_id)",
            }
        },
    ])


def _quota_count(table, user_id: str) -> int:
    item = _ensure_user_quota(table, user_id)
    return int(item.get("active_count", 0))


def _is_quota_transaction_failure(exc: ClientError) -> bool:
    reasons = exc.response.get("CancellationReasons", [])
    return bool(reasons and reasons[0].get("Code") == "ConditionalCheckFailed")


def _serialize_item(item: dict) -> dict:
    return {field: item[field] for field in PUBLIC_JOB_FIELDS if field in item}


def _upload_url_expired(item: dict) -> bool:
    try:
        created_at = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    safe_delete_after = PRESIGNED_URL_TTL_SECONDS + PRESIGNED_URL_DELETE_GRACE_SECONDS
    return datetime.now(timezone.utc) >= created_at + timedelta(seconds=safe_delete_after)


def _delete_s3_object(bucket: str, key: str) -> None:
    if not key:
        return
    try:
        s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404", "NotFound"}:
            raise


def _delete_s3_prefix(bucket: str, prefix: str) -> int:
    """Delete known job-scoped temporary objects without listing other prefixes."""
    if not re.fullmatch(r"checkpoints/[A-Za-z0-9_-]{1,100}/", prefix):
        raise ValueError("Invalid job checkpoint prefix")
    keys = []
    request = {"Bucket": bucket, "Prefix": prefix}
    while True:
        response = s3.list_objects_v2(**request)
        for item in response.get("Contents", []):
            key = item.get("Key")
            if key and key.startswith(prefix):
                keys.append(key)
        token = response.get("NextContinuationToken")
        if not response.get("IsTruncated") or not token:
            break
        request["ContinuationToken"] = token
    for key in keys:
        _delete_s3_object(bucket, key)
    return len(keys)


def _delete_job_questions(job_id: str, user_id: str) -> int:
    """Remove all transient question records for this job using its table partition key."""
    table_name = os.getenv("QUESTION_TABLE")
    if not table_name:
        return 0
    table = ddb.Table(table_name)
    query = {
        "KeyConditionExpression": Key("job_id").eq(job_id),
        "ConsistentRead": True,
    }
    deleted = 0
    while True:
        response = table.query(**query)
        for item in response.get("Items", []):
            if item.get("user_id") == user_id and item.get("ask_id"):
                table.delete_item(Key={"job_id": job_id, "ask_id": item["ask_id"]})
                deleted += 1
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return deleted
        query["ExclusiveStartKey"] = last_key


def _delete_job_transaction(table, job_id: str, user_id: str) -> None:
    _transact_write_items([
        {
            "Delete": {
                "TableName": table.name,
                "Key": {"job_id": job_id},
                "ConditionExpression": "user_id = :owner AND #status = :deleting",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {":owner": user_id, ":deleting": "DELETING"},
            }
        },
        {
            "Update": {
                "TableName": table.name,
                "Key": {"job_id": _quota_key(user_id)},
                "UpdateExpression": "ADD #count :decrement",
                "ConditionExpression": "#count > :zero",
                "ExpressionAttributeNames": {"#count": "active_count"},
                "ExpressionAttributeValues": {":decrement": -1, ":zero": 0},
            }
        },
    ])


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.getenv("CORS_ORIGIN", "http://localhost:5173"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
        },
        "body": json.dumps(body, ensure_ascii=False, default=_json_default),
    }


def _json_default(value):
    """Serialize DynamoDB Number values without changing the public API types."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(event: dict, user_id: str) -> dict:
    try:
        payload = json.loads(event.get("body") or "{}")
        filename = PurePath(str(payload.get("filename", ""))).name
        content_type = payload.get("content_type", "application/pdf")
        file_size = int(payload.get("file_size", 0))
        max_upload_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
        if not filename or not filename.lower().endswith(".pdf"):
            return _response(400, {"error": "Se requiere el nombre de un archivo PDF"})
        if file_size <= 0:
            return _response(400, {"error": "El PDF está vacío"})
        if file_size > max_upload_bytes:
            return _response(413, {"error": f"El archivo supera el límite de {max_upload_bytes} bytes"})
        job_id = str(uuid.uuid4())
        bucket = os.environ["DOCUMENTS_BUCKET"]
        key = f"users/{user_id}/jobs/{job_id}/{filename}"
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ContentType": content_type,
                "ContentLength": file_size,
                "Metadata": {"job-id": job_id},
            },
            ExpiresIn=900,
        )
        item = {
            "job_id": job_id,
            "user_id": user_id,
            "status": "UPLOAD_PENDING",
            "filename": filename,
            "bucket": bucket,
            "object_key": key,
            "created_at": _now(),
            "updated_at": _now(),
        }
        table = _table()
        _ensure_user_quota(table, user_id)
        try:
            _transact_create_job(table, item, user_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                current_count = _quota_count(table, user_id)
                if _is_quota_transaction_failure(exc) or current_count >= MAX_USER_JOBS:
                    return _response(409, {
                        "error": f"Has alcanzado el límite de {MAX_USER_JOBS} documentos. Elimina uno para continuar.",
                        "count": current_count,
                        "limit": MAX_USER_JOBS,
                    })
            raise
        count = _quota_count(table, user_id)
        return _response(201, {
            "job_id": job_id,
            "status": "UPLOAD_PENDING",
            "upload_url": upload_url,
            "count": count,
            "limit": MAX_USER_JOBS,
            "upload_headers": {
                "Content-Type": content_type,
                "x-amz-meta-job-id": job_id,
            },
        })
    except Exception:
        logger.exception("No se pudo crear el trabajo")
        return _response(500, {"error": "No se pudo crear el trabajo"})


def list_jobs(user_id: str) -> dict:
    try:
        table = _table()
        count = _quota_count(table, user_id)
        result = table.query(
            IndexName="UserJobsIndex",
            KeyConditionExpression=Key("user_id").eq(user_id),
            ScanIndexForward=False,
            Limit=USER_JOBS_PAGE_SIZE,
        )
        jobs = [_serialize_item(item) for item in result.get("Items", [])]
        return _response(200, {
            "jobs": jobs,
            "count": count,
            "limit": MAX_USER_JOBS,
            "has_more": "LastEvaluatedKey" in result,
        })
    except Exception:
        logger.exception("No se pudieron listar los trabajos del usuario")
        return _response(500, {"error": "No se pudieron listar los trabajos"})


def get_job(event: dict, user_id: str) -> dict:
    job_id = (event.get("pathParameters") or {}).get("job_id", "")
    if not job_id:
        return _response(400, {"error": "Falta job_id"})
    try:
        result = _table().get_item(Key={"job_id": job_id})
        item = result.get("Item")
        if not item or item.get("user_id") != user_id:
            return _response(404, {"error": "Trabajo no encontrado"})
        if item.get("status") == "UPLOAD_PENDING":
            item = _confirm_uploaded_object(_table(), item)
        item.pop("bucket", None)
        item.pop("object_key", None)
        item.pop("expires_at", None)
        item.pop("user_id", None)
        # Retain old prompt fields in stored records for worker compatibility,
        # but do not expose them through the current public job response.
        item.pop("user_prompt", None)
        for internal_field in (
            "processing_owner", "processing_message_id", "processing_lease_until", "processing_attempt",
            "checkpoint_manifest_key",
        ):
            item.pop(internal_field, None)
        result_key = item.pop("result_key", None)
        if result_key:
            try:
                result_object = s3.get_object(Bucket=os.environ["RESULTS_BUCKET"], Key=result_key)
                stored_result = json.loads(result_object["Body"].read())
                if isinstance(stored_result, dict):
                    # Page-scoped evidence is private backend context for /ask;
                    # the existing UI already receives the assembled transcription.
                    stored_result.pop("page_extractions", None)
                item["result"] = stored_result
            except ClientError as exc:
                error = exc.response.get("Error", {})
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if error.get("Code") in {"NoSuchKey", "NoSuchObject", "404", "NotFound"} or status == 404:
                    item["result_unavailable"] = True
                    item["result_message"] = "El resultado almacenado ya no está disponible."
                else:
                    raise
        return _response(200, item)
    except Exception:
        logger.exception("No se pudo consultar el trabajo job_id=%s", job_id)
        return _response(500, {"error": "No se pudo consultar el trabajo"})


def _ask_rate_key(user_id: str) -> dict:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {"job_id": f"__user_question_rate__#{user_id}#{day}", "ask_id": "__counter__"}


def _transact_create_ask(job_id: str, user_id: str, ask_item: dict, rate_key: dict) -> None:
    jobs_table = _table()
    question_table = _question_table()
    now = int(time.time())
    _transact_write_items([
        {
            "ConditionCheck": {
                "TableName": jobs_table.name,
                "Key": {"job_id": job_id},
                "ConditionExpression": "user_id = :owner AND #status = :completed",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {":owner": user_id, ":completed": "COMPLETED"},
            }
        },
        {
            "Update": {
                "TableName": question_table.name,
                "Key": rate_key,
                "UpdateExpression": "SET expires_at = :expires ADD #count :one",
                "ConditionExpression": "attribute_not_exists(#count) OR #count < :limit",
                "ExpressionAttributeNames": {"#count": "question_count"},
                "ExpressionAttributeValues": {
                    ":expires": now + QUESTION_RECORD_TTL_SECONDS,
                    ":one": 1,
                    ":limit": MAX_USER_QUESTIONS_PER_DAY,
                },
            }
        },
        {
            "Put": {
                "TableName": question_table.name,
                "Item": ask_item,
                "ConditionExpression": "attribute_not_exists(job_id) AND attribute_not_exists(ask_id)",
            }
        },
    ])


def _question_quota_count(rate_key: dict) -> int:
    item = _question_table().get_item(Key=rate_key, ConsistentRead=True).get("Item", {})
    return int(item.get("question_count", 0))


def _authorized_completed_job(job_id: str, user_id: str) -> tuple[dict | None, dict | None]:
    item = _table().get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item")
    if not item or item.get("user_id") != user_id:
        return None, _response(404, {"error": "Trabajo no encontrado"})
    if item.get("status") != "COMPLETED":
        if item.get("status") in {"DELETING"}:
            return None, _response(404, {"error": "Trabajo no encontrado"})
        return None, _response(409, {"error": "Las preguntas estarán disponibles cuando el análisis termine."})
    result_key = item.get("result_key")
    if result_key != f"results/{job_id}.json":
        return None, _response(409, {"error": "El resultado del documento no está disponible para consultas."})
    return item, None


def create_ask(event: dict, user_id: str) -> dict:
    job_id = (event.get("pathParameters") or {}).get("job_id", "")
    if not job_id:
        return _response(400, {"error": "Falta job_id"})
    try:
        payload = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return _response(400, {"error": "El cuerpo debe ser JSON válido"})
    if not isinstance(payload, dict):
        return _response(400, {"error": "El cuerpo debe ser un objeto JSON"})
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        return _response(400, {"error": "Escribe una pregunta sobre el documento"})
    question = question.strip()
    if len(question) > MAX_QUESTION_LENGTH:
        return _response(413, {"error": f"La pregunta no puede superar {MAX_QUESTION_LENGTH} caracteres"})

    _, error_response = _authorized_completed_job(job_id, user_id)
    if error_response:
        return error_response

    ask_id = str(uuid.uuid4())
    now = _now()
    expires_at = int(time.time()) + QUESTION_RECORD_TTL_SECONDS
    ask_item = {
        "job_id": job_id,
        "ask_id": ask_id,
        "user_id": user_id,
        "question": question,
        "status": "PENDING",
        "created_at": now,
        "updated_at": now,
        "expires_at": expires_at,
    }
    rate_key = _ask_rate_key(user_id)
    try:
        _transact_create_ask(job_id, user_id, ask_item, rate_key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            if _question_quota_count(rate_key) >= MAX_USER_QUESTIONS_PER_DAY:
                return _response(429, {"error": "Alcanzaste el límite diario de preguntas sobre documentos."})
            _, latest_error = _authorized_completed_job(job_id, user_id)
            if latest_error:
                return latest_error
            logger.info("No se pudo crear una consulta por conflicto de estado job_id=%s", job_id)
            return _response(409, {"error": "No se pudo iniciar la consulta. Inténtalo nuevamente."})
        logger.exception("No se pudo reservar una consulta job_id=%s", job_id)
        return _response(500, {"error": "No se pudo iniciar la consulta"})
    try:
        sqs.send_message(
            QueueUrl=os.environ["QUESTION_QUEUE_URL"],
            MessageBody=json.dumps({"job_id": job_id, "ask_id": ask_id, "user_id": user_id}),
        )
    except Exception:
        try:
            _question_table().update_item(
                Key={"job_id": job_id, "ask_id": ask_id},
                UpdateExpression="SET #status = :failed, error = :error, updated_at = :updated",
                ConditionExpression="#status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": "FAILED", ":pending": "PENDING",
                    ":error": "No se pudo poner la consulta en cola.", ":updated": _now(),
                },
            )
        except Exception:
            logger.exception("No se pudo cerrar una consulta que no llegó a SQS job_id=%s ask_id=%s", job_id, ask_id)
        logger.exception("No se pudo enviar una consulta a SQS job_id=%s ask_id=%s", job_id, ask_id)
        return _response(503, {"error": "No se pudo iniciar la consulta. Inténtalo nuevamente."})
    logger.info("ASK_CREATED job_id=%s ask_id=%s status=PENDING", job_id, ask_id)
    return _response(202, {"job_id": job_id, "ask_id": ask_id, "status": "PENDING"})


def get_ask(event: dict, user_id: str) -> dict:
    path = event.get("pathParameters") or {}
    job_id, ask_id = path.get("job_id", ""), path.get("ask_id", "")
    if not job_id or not ask_id:
        return _response(400, {"error": "Faltan identificadores de la consulta"})
    _, error_response = _authorized_completed_job(job_id, user_id)
    if error_response:
        return error_response
    try:
        item = _question_table().get_item(Key={"job_id": job_id, "ask_id": ask_id}, ConsistentRead=True).get("Item")
        if not item or item.get("user_id") != user_id:
            return _response(404, {"error": "Consulta no encontrada"})
        try:
            if int(item.get("expires_at", 0)) <= int(time.time()):
                return _response(404, {"error": "Consulta no encontrada"})
        except (TypeError, ValueError):
            return _response(404, {"error": "Consulta no encontrada"})
        # Recheck the parent after reading the answer. DELETE first changes the
        # job to DELETING, so a poll racing with deletion does not return data.
        _, latest_error = _authorized_completed_job(job_id, user_id)
        if latest_error:
            return latest_error
        if item.get("status") in {"PENDING", "PROCESSING"} and _ask_is_stale(item):
            _mark_stale_ask_failed(item, user_id)
            item = _question_table().get_item(
                Key={"job_id": job_id, "ask_id": ask_id}, ConsistentRead=True
            ).get("Item")
            if not item or item.get("user_id") != user_id:
                return _response(404, {"error": "Consulta no encontrada"})
        public_fields = ("job_id", "ask_id", "status", "answer", "source_pages", "context_limited", "created_at", "completed_at", "error")
        return _response(200, {field: item[field] for field in public_fields if field in item})
    except Exception:
        logger.exception("No se pudo consultar el estado de una pregunta job_id=%s ask_id=%s", job_id, ask_id)
        return _response(500, {"error": "No se pudo consultar la pregunta"})


def _ask_is_stale(item: dict) -> bool:
    timestamp = item.get("updated_at") or item.get("created_at")
    if not isinstance(timestamp, str):
        return False
    try:
        updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - updated).total_seconds() >= QUESTION_STALE_AFTER_SECONDS


def _mark_stale_ask_failed(item: dict, user_id: str) -> None:
    try:
        _question_table().update_item(
            Key={"job_id": item["job_id"], "ask_id": item["ask_id"]},
            UpdateExpression=(
                "SET #status = :failed, error = :error, error_category = :category, "
                "failed_at = :now, updated_at = :now "
                "REMOVE processing_owner, processing_lease_until"
            ),
            ConditionExpression=(
                "user_id = :owner AND expires_at > :epoch AND updated_at = :previous "
                "AND (#status = :pending OR #status = :processing)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":error": "La consulta excedió el tiempo máximo de recuperación. Puedes intentarlo nuevamente.",
                ":category": "stale_recovery_timeout",
                ":now": _now(),
                ":epoch": int(time.time()),
                ":previous": item.get("updated_at", item.get("created_at")),
                ":owner": user_id,
                ":pending": "PENDING",
                ":processing": "PROCESSING",
            },
        )
        logger.error("ASK_EXPIRED job_id=%s ask_id=%s outcome=marked_failed", item["job_id"], item["ask_id"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def delete_job(event: dict, user_id: str) -> dict:
    job_id = (event.get("pathParameters") or {}).get("job_id", "")
    if not job_id:
        return _response(400, {"error": "Falta job_id"})

    table = _table()
    item = table.get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item")
    if not item or item.get("user_id") != user_id:
        return _response(404, {"error": "Trabajo no encontrado"})

    status = item.get("status")
    if status == "UPLOAD_PENDING" and not _upload_url_expired(item):
        return _response(409, {"error": "La URL de subida todavía puede utilizarse. Intenta eliminar este trabajo después de 20 minutos."})
    if status not in {"COMPLETED", "FAILED", "DELETING", "UPLOAD_PENDING"}:
        return _response(409, {"error": "No se puede eliminar un trabajo mientras está en la cola o en procesamiento."})

    _ensure_user_quota(table, user_id)
    try:
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #status = :deleting, updated_at = :updated",
            ConditionExpression="user_id = :owner AND #status = :current",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":owner": user_id,
                ":current": status,
                ":deleting": "DELETING",
                ":updated": _now(),
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.exception("No se pudo marcar el trabajo para eliminación job_id=%s", job_id)
            return _response(500, {"error": "No se pudo iniciar la eliminación del trabajo"})
        latest = table.get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item")
        if not latest or latest.get("user_id") != user_id:
            return _response(404, {"error": "Trabajo no encontrado"})
        if latest.get("status") == "DELETING":
            item = latest
        else:
            return _response(409, {"error": "El estado del trabajo cambió. Actualiza el historial e inténtalo nuevamente."})

    result_key = item.get("result_key", "")
    object_key = item.get("object_key", "")
    if result_key and result_key != f"results/{job_id}.json":
        logger.error("El result_key tiene un prefijo no permitido job_id=%s", job_id)
        return _response(500, {"error": "La clave del resultado no es válida para eliminar"})
    if object_key and not (
        object_key.startswith(f"users/{user_id}/jobs/{job_id}/")
        or object_key.startswith(f"jobs/{job_id}/")
    ):
        logger.error("El object_key tiene un prefijo no permitido job_id=%s", job_id)
        return _response(500, {"error": "La clave del documento no es válida para eliminar"})

    try:
        deleted_questions = _delete_job_questions(job_id, user_id)
        logger.info("Deleted transient document questions job_id=%s count=%s", job_id, deleted_questions)
        if result_key:
            _delete_s3_object(os.environ["RESULTS_BUCKET"], result_key)
        deleted_checkpoints = _delete_s3_prefix(
            os.environ["RESULTS_BUCKET"], f"checkpoints/{job_id}/"
        )
        logger.info("Deleted job checkpoints job_id=%s count=%s", job_id, deleted_checkpoints)
        if object_key:
            _delete_s3_object(os.environ["DOCUMENTS_BUCKET"], object_key)
        try:
            _delete_job_transaction(table, job_id, user_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                latest = table.get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item")
                if not latest or latest.get("user_id") != user_id:
                    return _response(200, {
                        "deleted": True,
                        "job_id": job_id,
                        "count": _quota_count(table, user_id),
                        "limit": MAX_USER_JOBS,
                    })
            raise
        return _response(200, {
            "deleted": True,
            "job_id": job_id,
            "count": _quota_count(table, user_id),
            "limit": MAX_USER_JOBS,
        })
    except Exception:
        # Leave status=DELETING so the user can retry; S3 DeleteObject is idempotent.
        logger.exception("No se pudo completar la eliminación job_id=%s", job_id)
        return _response(500, {"error": "La eliminación quedó pendiente. Inténtalo nuevamente."})


def _confirm_uploaded_object(table, item: dict) -> dict:
    """Advance a job after GET observes its completed presigned S3 upload."""
    job_id = item["job_id"]
    try:
        uploaded = s3.head_object(Bucket=item["bucket"], Key=item["object_key"])
        if uploaded.get("Metadata", {}).get("job-id") != job_id:
            return item
        if uploaded.get("ContentLength", 0) <= 0:
            return item

        try:
            table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET #status = :queued, uploaded_at = :uploaded, updated_at = :updated",
                ConditionExpression="#status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": "UPLOAD_PENDING",
                    ":queued": "QUEUED",
                    ":uploaded": _now(),
                    ":updated": _now(),
                },
            )
            item["status"] = "QUEUED"
            item["uploaded_at"] = item["updated_at"] = _now()
            return item
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            # A queue consumer advanced the job after the read above.
            latest = table.get_item(Key={"job_id": job_id}).get("Item") or {}
            return latest or item
    except ClientError as exc:
        error = exc.response.get("Error", {})
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if error.get("Code") in {"NoSuchKey", "NoSuchObject", "404", "NotFound"} or status == 404:
            return item
        raise


def lambda_handler(event, context):
    method = event.get("httpMethod", "")
    path = event.get("resource", "")
    if method == "OPTIONS":
        return _response(204, {})
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    user_id = claims.get("sub")
    protected_paths = {
        "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/ask", "/jobs/{job_id}/asks/{ask_id}"
    }
    if method in {"POST", "GET", "DELETE"} and path in protected_paths and not user_id:
        return _response(401, {"error": "Autenticación requerida"})
    if method == "POST" and path == "/jobs":
        return create_job(event, user_id)
    if method == "GET" and path == "/jobs":
        return list_jobs(user_id)
    if method == "GET" and path == "/jobs/{job_id}":
        return get_job(event, user_id)
    if method == "DELETE" and path == "/jobs/{job_id}":
        return delete_job(event, user_id)
    if method == "POST" and path == "/jobs/{job_id}/ask":
        return create_ask(event, user_id)
    if method == "GET" and path == "/jobs/{job_id}/asks/{ask_id}":
        return get_ask(event, user_id)
    return _response(404, {"error": "Ruta no encontrada"})
