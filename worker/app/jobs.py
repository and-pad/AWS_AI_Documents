import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import PurePath

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
ddb = boto3.resource("dynamodb")
s3 = boto3.client("s3")


def _table():
    return ddb.Table(os.environ["JOBS_TABLE"])


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.getenv("CORS_ORIGIN", "http://localhost:5173"),
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(event: dict) -> dict:
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
        key = f"jobs/{job_id}/{filename}"
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
            "status": "UPLOAD_PENDING",
            "filename": filename,
            "bucket": bucket,
            "object_key": key,
            "user_prompt": user_prompt,
            "created_at": _now(),
            "updated_at": _now(),
            "expires_at": int(time.time()) + 30 * 24 * 60 * 60,
        }
        _table().put_item(Item=item)
        return _response(201, {
            "job_id": job_id,
            "status": "UPLOAD_PENDING",
            "upload_url": upload_url,
            "upload_headers": {
                "Content-Type": content_type,
                "x-amz-meta-job-id": job_id,
            },
        })
    except Exception:
        logger.exception("No se pudo crear el trabajo")
        return _response(500, {"error": "No se pudo crear el trabajo"})


def get_job(event: dict) -> dict:
    job_id = (event.get("pathParameters") or {}).get("job_id", "")
    if not job_id:
        return _response(400, {"error": "Falta job_id"})
    try:
        result = _table().get_item(Key={"job_id": job_id})
        item = result.get("Item")
        if not item:
            return _response(404, {"error": "Trabajo no encontrado"})
        if item.get("status") == "UPLOAD_PENDING":
            item = _confirm_uploaded_object(_table(), item)
        item.pop("bucket", None)
        item.pop("object_key", None)
        item.pop("expires_at", None)
        result_key = item.pop("result_key", None)
        if result_key:
            result_object = s3.get_object(Bucket=os.environ["RESULTS_BUCKET"], Key=result_key)
            item["result"] = json.loads(result_object["Body"].read())
        return _response(200, item)
    except Exception:
        logger.exception("No se pudo consultar el trabajo job_id=%s", job_id)
        return _response(500, {"error": "No se pudo consultar el trabajo"})


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
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return item
        raise


def lambda_handler(event, context):
    method = event.get("httpMethod", "")
    path = event.get("resource", "")
    if method == "OPTIONS":
        return _response(204, {})
    if method == "POST" and path == "/jobs":
        return create_job(event)
    if method == "GET" and path == "/jobs/{job_id}":
        return get_job(event)
    return _response(404, {"error": "Ruta no encontrada"})
