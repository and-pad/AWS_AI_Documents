import json
import logging
from io import BytesIO
from decimal import Decimal

from app import jobs
from botocore.exceptions import ClientError


class FakeTable:
    def __init__(self, item=None):
        self.item = item
        self.created = None

    def put_item(self, Item):
        self.created = Item

    def get_item(self, Key):
        return {"Item": self.item} if self.item else {}

    def query(self, **kwargs):
        self.query_params = kwargs
        expression = kwargs["KeyConditionExpression"].get_expression()
        user_id = expression["values"][1]
        return {"Items": [item for item in self.query_items if item.get("user_id") == user_id]}


def event(method, path, *, body=None, sub=None, job_id=None):
    claims = {"sub": sub} if sub else {}
    return {
        "httpMethod": method,
        "resource": path,
        "pathParameters": {"job_id": job_id} if job_id else {},
        "body": json.dumps(body or {}),
        "requestContext": {"authorizer": {"claims": claims}},
    }


def response_body(response):
    return json.loads(response["body"])


def test_create_job_uses_verified_sub_and_owner_scoped_s3_key(monkeypatch):
    table = FakeTable()
    captured = {}
    monkeypatch.setattr(jobs, "_table", lambda: table)
    monkeypatch.setattr(jobs, "_ensure_user_quota", lambda *_: {"active_count": 0})
    monkeypatch.setattr(jobs, "_transact_create_job", lambda target, item, user_id: setattr(target, "created", item))
    monkeypatch.setattr(jobs, "_quota_count", lambda *_: 1)
    monkeypatch.setattr(jobs.s3, "generate_presigned_url", lambda *args, **kwargs: captured.update(kwargs) or "https://upload")
    monkeypatch.setenv("DOCUMENTS_BUCKET", "documents")
    response = jobs.lambda_handler(event(
        "POST", "/jobs",
        body={"filename": "report.pdf", "file_size": 100, "user_id": "attacker"},
        sub="cognito-sub-a",
    ), None)

    assert response["statusCode"] == 201
    created = table.created
    assert created["user_id"] == "cognito-sub-a"
    assert "user_prompt" not in created
    assert created["object_key"].startswith(f"users/cognito-sub-a/jobs/{created['job_id']}/")
    assert "attacker" not in created["object_key"]
    assert captured["Params"]["Key"] == created["object_key"]


def test_job_owner_can_read_and_different_user_cannot(monkeypatch):
    table = FakeTable({"job_id": "job-1", "user_id": "owner-a", "status": "COMPLETED"})
    monkeypatch.setattr(jobs, "_table", lambda: table)

    own = jobs.lambda_handler(event("GET", "/jobs/{job_id}", sub="owner-a", job_id="job-1"), None)
    other = jobs.lambda_handler(event("GET", "/jobs/{job_id}", sub="owner-b", job_id="job-1"), None)

    assert own["statusCode"] == 200
    assert "user_id" not in response_body(own)
    assert other["statusCode"] == 404


def test_protected_job_routes_reject_missing_authentication(monkeypatch):
    def unexpected_table():
        raise AssertionError("DynamoDB must not be reached without a verified sub")

    monkeypatch.setattr(jobs, "_table", unexpected_table)
    post = jobs.lambda_handler(event("POST", "/jobs", body={"filename": "a.pdf", "file_size": 3}), None)
    get = jobs.lambda_handler(event("GET", "/jobs/{job_id}", job_id="job-1"), None)

    delete = jobs.lambda_handler(event("DELETE", "/jobs/{job_id}", job_id="job-1"), None)
    assert post["statusCode"] == get["statusCode"] == delete["statusCode"] == 401


def test_list_jobs_queries_user_index_latest_first_and_strips_internal_fields(monkeypatch):
    table = FakeTable()
    table.query_items = [
        {
            "job_id": "newer",
            "user_id": "owner-a",
            "created_at": "2026-10-02T00:00:00+00:00",
            "status": "PROCESSING",
            "current_page": Decimal("2"),
            "total_pages": Decimal("3"),
            "progress_percent": Decimal("66"),
            "bucket": "private-bucket",
            "object_key": "users/owner-a/jobs/newer/file.pdf",
            "user_prompt": "private prompt",
            "result_key": "results/newer.json",
        },
        {
            "job_id": "older",
            "user_id": "owner-a",
            "created_at": "2026-10-01T00:00:00+00:00",
            "status": "COMPLETED",
            "filename": "older.pdf",
        },
        {
            "job_id": "other-user-job",
            "user_id": "owner-b",
            "created_at": "2026-10-03T00:00:00+00:00",
            "status": "COMPLETED",
        },
    ]
    monkeypatch.setattr(jobs, "_table", lambda: table)
    monkeypatch.setattr(jobs, "_quota_count", lambda *_: 2)

    response = jobs.lambda_handler(event("GET", "/jobs", sub="owner-a"), None)
    body = response_body(response)

    assert response["statusCode"] == 200
    assert [job["job_id"] for job in body["jobs"]] == ["newer", "older"]
    assert "bucket" not in body["jobs"][0]
    assert "object_key" not in body["jobs"][0]
    assert "user_id" not in body["jobs"][0]
    assert "user_prompt" not in body["jobs"][0]
    assert "result_key" not in body["jobs"][0]
    assert body["jobs"][0]["current_page"] == 2
    assert body["jobs"][0]["total_pages"] == 3
    assert body["jobs"][0]["progress_percent"] == 66
    assert table.query_params["IndexName"] == "UserJobsIndex"
    assert table.query_params["ScanIndexForward"] is False
    assert table.query_params["Limit"] == jobs.USER_JOBS_PAGE_SIZE
    assert table.query_params["KeyConditionExpression"].get_expression()["values"][1] == "owner-a"


def test_list_jobs_for_user_without_jobs_returns_empty(monkeypatch):
    table = FakeTable()
    table.query_items = []
    monkeypatch.setattr(jobs, "_table", lambda: table)
    monkeypatch.setattr(jobs, "_quota_count", lambda *_: 0)

    response = jobs.lambda_handler(event("GET", "/jobs", sub="owner-a"), None)

    assert response["statusCode"] == 200
    assert response_body(response)["jobs"] == []


def test_response_serializes_dynamodb_decimals_recursively():
    response = jobs._response(200, {
        "integral": Decimal("10"),
        "fractional": Decimal("10.5"),
        "nested": {"list": [Decimal("2"), {"ratio": Decimal("0.25")}]} ,
    })

    assert response_body(response) == {
        "integral": 10,
        "fractional": 10.5,
        "nested": {"list": [2, {"ratio": 0.25}]},
    }


def test_list_jobs_rejects_missing_sub(monkeypatch):
    def unexpected_table():
        raise AssertionError("DynamoDB must not be reached without a verified sub")

    monkeypatch.setattr(jobs, "_table", unexpected_table)
    response = jobs.lambda_handler(event("GET", "/jobs"), None)

    assert response["statusCode"] == 401


def test_completed_job_with_missing_result_returns_metadata_and_unavailable_message(monkeypatch):
    table = FakeTable({
        "job_id": "job-1",
        "user_id": "owner-a",
        "status": "COMPLETED",
        "filename": "report.pdf",
        "result_key": "results/job-1.json",
    })
    monkeypatch.setattr(jobs, "_table", lambda: table)
    monkeypatch.setenv("RESULTS_BUCKET", "results")

    def missing_result(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "missing"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "GetObject",
        )

    monkeypatch.setattr(jobs.s3, "get_object", missing_result)

    response = jobs.lambda_handler(event("GET", "/jobs/{job_id}", sub="owner-a", job_id="job-1"), None)
    body = response_body(response)

    assert response["statusCode"] == 200
    assert body["status"] == "COMPLETED"
    assert body["filename"] == "report.pdf"
    assert body["result_unavailable"] is True
    assert body["result_message"] == "El resultado almacenado ya no está disponible."
    assert "result" not in body


def test_result_access_denied_remains_an_error_and_is_logged(monkeypatch, caplog):
    table = FakeTable({
        "job_id": "job-1",
        "user_id": "owner-a",
        "status": "COMPLETED",
        "result_key": "results/job-1.json",
    })
    monkeypatch.setattr(jobs, "_table", lambda: table)

    def forbidden(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "GetObject",
        )

    monkeypatch.setattr(jobs.s3, "get_object", forbidden)
    with caplog.at_level(logging.ERROR):
        response = jobs.lambda_handler(event("GET", "/jobs/{job_id}", sub="owner-a", job_id="job-1"), None)

    assert response["statusCode"] == 500
    assert "No se pudo consultar el trabajo" in caplog.text


def test_upload_pending_missing_document_remains_pending(monkeypatch):
    table = FakeTable({
        "job_id": "job-pending",
        "user_id": "owner-a",
        "status": "UPLOAD_PENDING",
        "filename": "report.pdf",
        "bucket": "documents",
        "object_key": "users/owner-a/jobs/job-pending/report.pdf",
    })
    monkeypatch.setattr(jobs, "_table", lambda: table)

    def missing_document(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "404", "Message": "missing"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )

    monkeypatch.setattr(jobs.s3, "head_object", missing_document)

    response = jobs.lambda_handler(event("GET", "/jobs/{job_id}", sub="owner-a", job_id="job-pending"), None)

    assert response["statusCode"] == 200
    assert response_body(response)["status"] == "UPLOAD_PENDING"


def test_upload_pending_head_access_denied_is_not_treated_as_missing(monkeypatch):
    table = FakeTable({
        "job_id": "job-pending",
        "user_id": "owner-a",
        "status": "UPLOAD_PENDING",
        "bucket": "documents",
        "object_key": "users/owner-a/jobs/job-pending/report.pdf",
    })
    monkeypatch.setattr(jobs, "_table", lambda: table)

    def forbidden(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "HeadObject",
        )

    monkeypatch.setattr(jobs.s3, "head_object", forbidden)

    response = jobs.lambda_handler(event("GET", "/jobs/{job_id}", sub="owner-a", job_id="job-pending"), None)

    assert response["statusCode"] == 500


def test_get_job_keeps_page_extractions_private_to_backend_context(monkeypatch):
    table = FakeTable({
        "job_id": "job-with-pages", "user_id": "owner-a", "status": "COMPLETED",
        "result_key": "results/job-with-pages.json",
    })
    stored = {
        "filename": "report.pdf", "pages": 1,
        "transcription": {"text": "--- Página 1 ---\nContenido"},
        "page_extractions": [{"page": 1, "transcription": "Contenido", "analysis_notes": ""}],
    }
    monkeypatch.setattr(jobs, "_table", lambda: table)
    monkeypatch.setenv("RESULTS_BUCKET", "results")
    monkeypatch.setattr(jobs.s3, "get_object", lambda **_kwargs: {"Body": BytesIO(json.dumps(stored).encode())})

    response = jobs.lambda_handler(event("GET", "/jobs/{job_id}", sub="owner-a", job_id="job-with-pages"), None)

    assert response["statusCode"] == 200
    result = response_body(response)["result"]
    assert result["transcription"]["text"].startswith("--- Página 1 ---")
    assert "page_extractions" not in result
