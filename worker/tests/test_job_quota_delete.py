import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from app import jobs


class MemoryS3:
    def __init__(self):
        self.objects = set()
        self.deleted = []
        self.listed_prefixes = []

    def generate_presigned_url(self, *_args, **_kwargs):
        return "https://upload.example.test"

    def delete_object(self, *, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.discard((Bucket, Key))
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, **_kwargs):
        self.listed_prefixes.append((Bucket, Prefix))
        keys = [key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix)]
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


class MemoryTable:
    name = "jobs"

    def __init__(self, jobs_list=None):
        self.jobs = {item["job_id"]: dict(item) for item in (jobs_list or [])}
        self.quotas = {}
        self.lock = threading.Lock()

    def get_item(self, *, Key, **_kwargs):
        job_id = Key["job_id"]
        with self.lock:
            item = self.quotas.get(job_id) or self.jobs.get(job_id)
            return {"Item": dict(item)} if item else {}

    def query(self, **kwargs):
        expression = kwargs["KeyConditionExpression"].get_expression()
        user_id = expression["values"][1]
        items = [item for item in self.jobs.values() if item.get("user_id") == user_id]
        if kwargs.get("Select") == "COUNT":
            return {"Count": len(items)}
        items.sort(key=lambda item: item["created_at"], reverse=not kwargs.get("ScanIndexForward", True))
        limit = kwargs.get("Limit", len(items))
        page = items[:limit]
        response = {"Items": page, "Count": len(page)}
        if len(items) > limit:
            response["LastEvaluatedKey"] = {"job_id": page[-1]["job_id"]}
        return response

    def put_item(self, *, Item, **_kwargs):
        with self.lock:
            if Item["job_id"] in self.quotas or Item["job_id"] in self.jobs:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
            self.quotas[Item["job_id"]] = dict(Item)
        return {}

    def update_item(self, *, Key, ExpressionAttributeValues, **_kwargs):
        with self.lock:
            item = self.jobs.get(Key["job_id"])
            if (
                not item
                or item.get("user_id") != ExpressionAttributeValues[":owner"]
                or item.get("status") != ExpressionAttributeValues[":current"]
            ):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
            item["status"] = ExpressionAttributeValues[":deleting"]
            item["updated_at"] = ExpressionAttributeValues[":updated"]
        return {}

    def transact_write_items(self, transact_items):
        with self.lock:
            first, second = transact_items
            if "Put" in second:
                quota_update, job_put = first["Update"], second["Put"]
                quota_key = quota_update["Key"]["job_id"]
                values = quota_update["ExpressionAttributeValues"]
                quota = self.quotas[quota_key]
                if quota["active_count"] >= values[":limit"]:
                    raise ClientError({
                        "Error": {"Code": "TransactionCanceledException"},
                        "CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
                    }, "TransactWriteItems")
                item = job_put["Item"]
                quota["active_count"] += values[":one"]
                self.jobs[item["job_id"]] = item
                return {}

            job_delete, quota_update = first["Delete"], second["Update"]
            job_id = job_delete["Key"]["job_id"]
            values = job_delete["ExpressionAttributeValues"]
            quota_key = quota_update["Key"]["job_id"]
            quota_values = quota_update["ExpressionAttributeValues"]
            item = self.jobs.get(job_id)
            quota = self.quotas[quota_key]
            if not item or item.get("user_id") != values[":owner"] or item.get("status") != "DELETING":
                raise ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
            del self.jobs[job_id]
            quota["active_count"] += quota_values[":decrement"]
            return {}


class MemoryTableFactory:
    def __init__(self, table):
        self.table = table
        self.meta = type("Meta", (), {"client": self})()

    def Table(self, _name):
        return self.table

    def transact_write_items(self, *, TransactItems):
        return self.table.transact_write_items(TransactItems)


def request(method, route, *, sub="user-a", job_id=None, body=None):
    return {
        "httpMethod": method,
        "resource": route,
        "pathParameters": {"job_id": job_id} if job_id else {},
        "body": json.dumps(body or {}),
        "requestContext": {"authorizer": {"claims": {"sub": sub}}},
    }


def old_job(job_id, *, user_id="user-a", status="COMPLETED", result_key=None, object_key=None):
    return {
        "job_id": job_id,
        "user_id": user_id,
        "status": status,
        "filename": f"{job_id}.pdf",
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "updated_at": "2026-10-01T00:00:00+00:00",
        "result_key": result_key,
        "object_key": object_key,
        "bucket": "documents",
    }


def setup(monkeypatch, table, s3=None):
    s3 = s3 or MemoryS3()
    client = MemoryTableFactory(table)
    monkeypatch.setattr(jobs, "ddb", client)
    monkeypatch.setattr(jobs, "_table", lambda: table)
    monkeypatch.setattr(jobs, "_transact_write_items", lambda items: client.transact_write_items(TransactItems=items))
    monkeypatch.setattr(jobs, "s3", s3)
    monkeypatch.setattr(jobs, "MAX_USER_JOBS", 20)
    monkeypatch.setenv("DOCUMENTS_BUCKET", "documents")
    monkeypatch.setenv("RESULTS_BUCKET", "results")
    return s3


def test_user_with_19_jobs_can_create_the_twentieth(monkeypatch):
    existing = [old_job(f"job-{index}") for index in range(19)]
    table = MemoryTable(existing)
    setup(monkeypatch, table)

    response = jobs.lambda_handler(request("POST", "/jobs", body={"filename": "new.pdf", "file_size": 10}), None)

    assert response["statusCode"] == 201
    assert table.quotas[jobs._quota_key("user-a")]["active_count"] == 20
    assert len([item for item in table.jobs.values() if item.get("user_id") == "user-a"]) == 20


def test_user_with_20_jobs_cannot_create_job_21(monkeypatch):
    table = MemoryTable([old_job(f"job-{index}") for index in range(20)])
    setup(monkeypatch, table)

    response = jobs.lambda_handler(request("POST", "/jobs", body={"filename": "new.pdf", "file_size": 10}), None)

    assert response["statusCode"] == 409
    assert "límite de 20" in json.loads(response["body"])["error"]
    assert table.quotas[jobs._quota_key("user-a")]["active_count"] == 20


def test_concurrent_create_requests_cannot_exceed_quota(monkeypatch):
    table = MemoryTable([old_job(f"job-{index}") for index in range(19)])
    setup(monkeypatch, table)

    def create(_index):
        return jobs.lambda_handler(request("POST", "/jobs", body={"filename": "new.pdf", "file_size": 10}), None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(create, range(2)))

    assert sorted(response["statusCode"] for response in responses) == [201, 409]
    assert table.quotas[jobs._quota_key("user-a")]["active_count"] == 20
    assert len([item for item in table.jobs.values() if item.get("user_id") == "user-a"]) == 20


def test_delete_removes_results_input_and_job_then_releases_quota(monkeypatch):
    item = old_job(
        "job-delete",
        result_key="results/job-delete.json",
        object_key="users/user-a/jobs/job-delete/file.pdf",
    )
    table = MemoryTable([item])
    s3 = setup(monkeypatch, table)
    s3.objects.update({("results", item["result_key"]), ("documents", item["object_key"])})

    response = jobs.lambda_handler(request("DELETE", "/jobs/{job_id}", job_id="job-delete"), None)
    listing = jobs.lambda_handler(request("GET", "/jobs"), None)

    assert response["statusCode"] == 200
    assert s3.deleted == [
        ("results", "results/job-delete.json"),
        ("documents", "users/user-a/jobs/job-delete/file.pdf"),
    ]
    assert "job-delete" not in table.jobs
    assert table.quotas[jobs._quota_key("user-a")]["active_count"] == 0
    assert json.loads(listing["body"])["jobs"] == []
    assert json.loads(listing["body"])["count"] == 0


def test_delete_also_removes_only_job_checkpoint_prefix(monkeypatch):
    item = old_job("job-checkpoints", result_key="results/job-checkpoints.json")
    table = MemoryTable([item])
    s3 = setup(monkeypatch, table)
    s3.objects.update({
        ("results", "results/job-checkpoints.json"),
        ("results", "checkpoints/job-checkpoints/page-0001/owner-a.json"),
        ("results", "checkpoints/job-other/page-0001/owner-b.json"),
    })

    response = jobs.lambda_handler(request("DELETE", "/jobs/{job_id}", job_id=item["job_id"]), None)

    assert response["statusCode"] == 200
    assert ("results", "checkpoints/job-checkpoints/page-0001/owner-a.json") in s3.deleted
    assert ("results", "checkpoints/job-other/page-0001/owner-b.json") not in s3.deleted
    assert s3.listed_prefixes == [("results", "checkpoints/job-checkpoints/")]


def test_user_cannot_delete_another_users_job_or_s3_objects(monkeypatch):
    item = old_job("job-owned-by-b", user_id="user-b", result_key="results/job-owned-by-b.json")
    table = MemoryTable([item])
    s3 = setup(monkeypatch, table)

    response = jobs.lambda_handler(request("DELETE", "/jobs/{job_id}", job_id=item["job_id"], sub="user-a"), None)

    assert response["statusCode"] == 404
    assert not s3.deleted
    assert item["job_id"] in table.jobs


def test_internal_processing_lease_fields_are_not_returned_by_get_job(monkeypatch):
    item = old_job("job-internal-fields")
    item.update({
        "processing_owner": "opaque-owner-token",
        "processing_message_id": "sqs-message",
        "processing_lease_until": "2026-10-20T00:00:00Z",
        "processing_attempt": 2,
        "checkpoint_manifest_key": "checkpoints/job-internal-fields/manifest/owner.json",
    })
    table = MemoryTable([item])
    setup(monkeypatch, table)

    response = jobs.lambda_handler(request("GET", "/jobs/{job_id}", job_id=item["job_id"]), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert not {
        "processing_owner", "processing_message_id", "processing_lease_until", "processing_attempt",
        "checkpoint_manifest_key",
    }.intersection(body)


def test_processing_job_cannot_be_deleted(monkeypatch):
    item = old_job("job-processing", status="PROCESSING", result_key="results/job-processing.json")
    table = MemoryTable([item])
    s3 = setup(monkeypatch, table)

    response = jobs.lambda_handler(request("DELETE", "/jobs/{job_id}", job_id=item["job_id"]), None)

    assert response["statusCode"] == 409
    assert not s3.deleted
    assert table.jobs[item["job_id"]]["status"] == "PROCESSING"


def test_missing_s3_objects_do_not_prevent_job_deletion(monkeypatch):
    item = old_job("job-no-objects", result_key="results/job-no-objects.json", object_key="users/user-a/jobs/job-no-objects/file.pdf")
    table = MemoryTable([item])
    s3 = setup(monkeypatch, table)

    response = jobs.lambda_handler(request("DELETE", "/jobs/{job_id}", job_id=item["job_id"]), None)

    assert response["statusCode"] == 200
    assert item["job_id"] not in table.jobs
    assert table.quotas[jobs._quota_key("user-a")]["active_count"] == 0


def test_upload_pending_cannot_be_deleted_while_presigned_url_is_valid(monkeypatch):
    item = old_job("job-pending", status="UPLOAD_PENDING", object_key="users/user-a/jobs/job-pending/file.pdf")
    item["created_at"] = datetime.now(timezone.utc).isoformat()
    table = MemoryTable([item])
    s3 = setup(monkeypatch, table)

    response = jobs.lambda_handler(request("DELETE", "/jobs/{job_id}", job_id=item["job_id"]), None)

    assert response["statusCode"] == 409
    assert not s3.deleted
    assert item["job_id"] in table.jobs


def test_expired_upload_pending_can_be_deleted_with_any_uploaded_pdf(monkeypatch):
    item = old_job(
        "job-pending-expired",
        status="UPLOAD_PENDING",
        object_key="users/user-a/jobs/job-pending-expired/file.pdf",
    )
    item["created_at"] = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    table = MemoryTable([item])
    s3 = setup(monkeypatch, table)
    s3.objects.add(("documents", item["object_key"]))

    response = jobs.lambda_handler(request("DELETE", "/jobs/{job_id}", job_id=item["job_id"]), None)

    assert response["statusCode"] == 200
    assert s3.deleted == [("documents", item["object_key"])]
    assert item["job_id"] not in table.jobs


def test_failed_s3_delete_keeps_job_and_slot_for_safe_retry(monkeypatch):
    item = old_job(
        "job-retry-delete",
        result_key="results/job-retry-delete.json",
        object_key="users/user-a/jobs/job-retry-delete/file.pdf",
    )
    table = MemoryTable([item])

    class FailOnceS3(MemoryS3):
        failed = False

        def delete_object(self, *, Bucket, Key):
            if Bucket == "documents" and not self.failed:
                self.failed = True
                raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "temporary"}}, "DeleteObject")
            return super().delete_object(Bucket=Bucket, Key=Key)

    s3 = setup(monkeypatch, table, FailOnceS3())
    s3.objects.update({("results", item["result_key"]), ("documents", item["object_key"])})

    first = jobs.lambda_handler(request("DELETE", "/jobs/{job_id}", job_id=item["job_id"]), None)
    assert first["statusCode"] == 500
    assert table.jobs[item["job_id"]]["status"] == "DELETING"
    assert table.quotas[jobs._quota_key("user-a")]["active_count"] == 1

    retry = jobs.lambda_handler(request("DELETE", "/jobs/{job_id}", job_id=item["job_id"]), None)
    assert retry["statusCode"] == 200
    assert item["job_id"] not in table.jobs
    assert table.quotas[jobs._quota_key("user-a")]["active_count"] == 0
