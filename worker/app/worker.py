import json
import logging
import os
import time
from urllib.parse import unquote_plus

import boto3

from app.ai_client import send_document
from app.s3 import download_object

logger = logging.getLogger()
logger.setLevel(logging.INFO)
dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")


def _update_job(job_id: str, status: str, **attributes) -> None:
    if not job_id or not os.getenv("JOBS_TABLE"):
        return
    names = {"#status": "status"}
    values = {":status": status, ":updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    assignments = ["#status = :status", "updated_at = :updated"]
    for index, (key, value) in enumerate(attributes.items()):
        name_token, value_token = f"#field{index}", f":value{index}"
        names[name_token] = key
        values[value_token] = value
        assignments.append(f"{name_token} = {value_token}")
    try:
        dynamodb.Table(os.environ["JOBS_TABLE"]).update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:
        logger.exception("Could not update job status job_id=%s status=%s", job_id, status)
        raise


def _get_user_prompt(job_id: str) -> str:
    if not job_id or not os.getenv("JOBS_TABLE"):
        return ""
    response = dynamodb.Table(os.environ["JOBS_TABLE"]).get_item(Key={"job_id": job_id})
    return (response.get("Item") or {}).get("user_prompt", "")


def _process_record(record: dict) -> None:
    body = json.loads(record["body"])
    for s3_record in body.get("Records", []):
        bucket = s3_record["s3"]["bucket"]["name"]
        key = unquote_plus(s3_record["s3"]["object"]["key"])
        key_parts = key.split("/", 2)
        job_id = key_parts[1] if len(key_parts) > 2 and key_parts[0] == "jobs" else ""
        try:
            object_info = s3_client.head_object(Bucket=bucket, Key=key)
            metadata = object_info.get("Metadata", {})
            job_id = metadata.get("job-id", job_id)
            prompt = _get_user_prompt(job_id)
            # JobsApi marks a verified browser upload as QUEUED immediately.
            # Keep this update for older clients/jobs that did not confirm it.
            _update_job(job_id, "QUEUED")
            _update_job(job_id, "PROCESSING", started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            max_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
            if object_info.get("ContentLength", 0) > max_bytes:
                raise ValueError(f"Document exceeds configured upload limit ({max_bytes} bytes)")
            document = download_object(bucket, key)
            filename = key.rsplit("/", 1)[-1]
            result = send_document(filename, "application/pdf", document, prompt)
            if job_id:
                result_key = f"results/{job_id}.json"
                s3_client.put_object(
                    Bucket=os.environ["RESULTS_BUCKET"],
                    Key=result_key,
                    Body=json.dumps(result, ensure_ascii=False).encode("utf-8"),
                    ContentType="application/json; charset=utf-8",
                )
                _update_job(job_id, "COMPLETED", result_key=result_key, completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            logger.info("Document processed successfully key=%s job_id=%s", key, job_id or "legacy")
        except Exception:
            receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
            if job_id and receive_count >= 3:
                _update_job(job_id, "FAILED", error="No se pudo procesar el documento", failed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            elif job_id:
                _update_job(job_id, "QUEUED", last_error="El procesamiento falló; se reintentará")
            logger.exception("Document processing failed key=%s attempt=%d", key, receive_count)
            raise


def lambda_handler(event, context):
    logger.info("Lambda recibió %d mensaje(s) SQS", len(event.get("Records", [])))
    failures = []
    for record in event.get("Records", []):
        try:
            _process_record(record)
        except Exception:
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
