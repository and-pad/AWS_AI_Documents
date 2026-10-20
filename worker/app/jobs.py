import json
import logging
import os
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
USER_JOBS_PAGE_SIZE = 100
MAX_USER_JOBS = int(os.getenv("MAX_USER_JOBS", "20"))
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
            "Access-Control-Allow-Headers": "content-type,authorization",
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
        user_prompt = str(payload.get("user_prompt") or "")[:4000]
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
            "user_prompt": user_prompt,
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
        result_key = item.pop("result_key", None)
        if result_key:
            try:
                result_object = s3.get_object(Bucket=os.environ["RESULTS_BUCKET"], Key=result_key)
                item["result"] = json.loads(result_object["Body"].read())
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
        if result_key:
            _delete_s3_object(os.environ["RESULTS_BUCKET"], result_key)
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
    if method in {"POST", "GET", "DELETE"} and path in {"/jobs", "/jobs/{job_id}"} and not user_id:
        return _response(401, {"error": "Autenticación requerida"})
    if method == "POST" and path == "/jobs":
        return create_job(event, user_id)
    if method == "GET" and path == "/jobs":
        return list_jobs(user_id)
    if method == "GET" and path == "/jobs/{job_id}":
        return get_job(event, user_id)
    if method == "DELETE" and path == "/jobs/{job_id}":
        return delete_job(event, user_id)
    return _response(404, {"error": "Ruta no encontrada"})
