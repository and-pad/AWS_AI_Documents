import json
from pathlib import Path

import pytest

from app import worker


class FakeS3:
    def __init__(self):
        self.put = None
        self.puts = []
        self.deleted = []
        self.events = []

    def head_object(self, **kwargs):
        return {
            "ContentLength": 25,
            "Metadata": {"job-id": worker._job_id_from_key(kwargs["Key"])},
        }

    def put_object(self, **kwargs):
        self.events.append("result")
        self.put = kwargs
        self.puts.append(kwargs)

    def delete_object(self, **kwargs):
        self.events.append("delete_source")
        self.deleted.append(kwargs)


def test_sqs_s3_event_processes_owner_scoped_key_through_result(monkeypatch):
    s3 = FakeS3()
    statuses = []
    monkeypatch.setattr(worker, "s3_client", s3)
    monkeypatch.setattr(worker, "download_object", lambda bucket, key: b"pdf-bytes")
    monkeypatch.setattr(worker, "send_document", lambda filename, content_type, data, prompt: {"filename": filename, "final_analysis": "ok"})
    monkeypatch.setattr(worker, "_get_user_prompt", lambda job_id: "extract totals")
    def update_job(job_id, status, **attrs):
        statuses.append((job_id, status, attrs))
        if status == "COMPLETED":
            s3.events.append("completed")
        return True

    monkeypatch.setattr(worker, "_update_job", update_job)
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    monkeypatch.setenv("RESULTS_BUCKET", "results")
    key = "users/user-sub/jobs/job-2/report.pdf"
    record = {
        "messageId": "message-1",
        "attributes": {"ApproximateReceiveCount": "1"},
        "body": json.dumps({"Records": [{"s3": {"bucket": {"name": "documents"}, "object": {"key": key}}}]}),
    }

    worker._process_record(record)

    assert [status for _, status, _ in statuses] == ["QUEUED", "PROCESSING", "COMPLETED"]
    assert s3.put["Bucket"] == "results"
    assert s3.put["Key"] == "results/job-2.json"
    assert json.loads(s3.put["Body"])["final_analysis"] == "ok"
    assert s3.deleted == [{"Bucket": "documents", "Key": key}]
    assert s3.events == ["result", "completed", "delete_source"]


def test_worker_retains_pdf_when_processing_fails_before_result_is_saved(monkeypatch):
    s3 = FakeS3()
    statuses = []
    monkeypatch.setattr(worker, "s3_client", s3)
    monkeypatch.setattr(worker, "download_object", lambda bucket, key: b"pdf-bytes")
    monkeypatch.setattr(worker, "send_document", lambda *_args: (_ for _ in ()).throw(RuntimeError("processor failed")))
    monkeypatch.setattr(worker, "_get_user_prompt", lambda _job_id: "")

    def update_job(job_id, status, **attrs):
        statuses.append((job_id, status, attrs))
        return True

    monkeypatch.setattr(worker, "_update_job", update_job)
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    monkeypatch.setenv("RESULTS_BUCKET", "results")
    key = "users/user-sub/jobs/job-failed/report.pdf"
    record = {
        "messageId": "message-failed",
        "attributes": {"ApproximateReceiveCount": "1"},
        "body": json.dumps({"Records": [{"s3": {"bucket": {"name": "documents"}, "object": {"key": key}}}]}),
    }

    with pytest.raises(RuntimeError, match="processor failed"):
        worker._process_record(record)

    assert s3.put is None
    assert s3.deleted == []
    assert [status for _, status, _ in statuses] == ["QUEUED", "PROCESSING", "QUEUED"]


def test_worker_retains_pdf_when_completed_state_is_not_persisted(monkeypatch):
    s3 = FakeS3()
    statuses = []
    monkeypatch.setattr(worker, "s3_client", s3)
    monkeypatch.setattr(worker, "download_object", lambda bucket, key: b"pdf-bytes")
    monkeypatch.setattr(worker, "send_document", lambda *_args: {"final_analysis": "ok"})
    monkeypatch.setattr(worker, "_get_user_prompt", lambda _job_id: "")

    def update_job(job_id, status, **attrs):
        statuses.append((job_id, status, attrs))
        return status != "COMPLETED"

    monkeypatch.setattr(worker, "_update_job", update_job)
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    monkeypatch.setenv("RESULTS_BUCKET", "results")
    key = "users/user-sub/jobs/job-state-failed/report.pdf"
    record = {
        "messageId": "message-state-failed",
        "attributes": {"ApproximateReceiveCount": "1"},
        "body": json.dumps({"Records": [{"s3": {"bucket": {"name": "documents"}, "object": {"key": key}}}]}),
    }

    worker._process_record(record)

    assert s3.put is not None
    assert s3.deleted == []
    assert s3.events == ["result"]
    assert [status for _, status, _ in statuses] == ["QUEUED", "PROCESSING", "COMPLETED"]


def test_retry_after_result_put_overwrites_same_deterministic_key(monkeypatch):
    s3 = FakeS3()
    state = {"status": "UPLOAD_PENDING", "completed_attempts": 0}

    def update_job(_job_id, status, **_attrs):
        if status == "QUEUED":
            state["status"] = status
            return True
        if status == "PROCESSING":
            state["status"] = status
            return True
        if status == "COMPLETED":
            state["completed_attempts"] += 1
            if state["completed_attempts"] == 1:
                raise RuntimeError("temporary DynamoDB failure after result put")
            state["status"] = status
            return True
        return True

    monkeypatch.setattr(worker, "s3_client", s3)
    monkeypatch.setattr(worker, "download_object", lambda _bucket, _key: b"pdf-bytes")
    monkeypatch.setattr(worker, "send_document", lambda *_args: {"final_analysis": "ok"})
    monkeypatch.setattr(worker, "_get_user_prompt", lambda _job_id: "")
    monkeypatch.setattr(worker, "_update_job", update_job)
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    monkeypatch.setenv("RESULTS_BUCKET", "results")
    key = "users/user-sub/jobs/job-retry/report.pdf"
    record = {
        "messageId": "message-retry",
        "attributes": {"ApproximateReceiveCount": "1"},
        "body": json.dumps({"Records": [{"s3": {"bucket": {"name": "documents"}, "object": {"key": key}}}]}),
    }

    with pytest.raises(RuntimeError, match="temporary DynamoDB failure"):
        worker._process_record(record)
    assert s3.deleted == []
    assert state["status"] == "QUEUED"

    record["attributes"]["ApproximateReceiveCount"] = "2"
    worker._process_record(record)

    result_keys = [(put["Bucket"], put["Key"]) for put in s3.puts]
    assert result_keys == [
        ("results", "results/job-retry.json"),
        ("results", "results/job-retry.json"),
    ]
    assert state["status"] == "COMPLETED"
    assert s3.deleted == [{"Bucket": "documents", "Key": key}]


def test_documents_lifecycle_is_temporary_and_results_bucket_has_no_short_lifecycle():
    storage = Path(__file__).parents[2].joinpath("infrastructure/aws/stacks/storage.yml").read_text()
    documents_bucket = storage.split("  DocumentsBucket:\n", 1)[1].split("\n  ResultsBucket:", 1)[0]
    results_bucket = storage.split("  ResultsBucket:\n", 1)[1].split("\n  JobsTable:", 1)[0]

    assert "LifecycleConfiguration:" in documents_bucket
    assert "ExpirationInDays: 30" in documents_bucket
    assert "LifecycleConfiguration:" not in results_bucket


def test_worker_keeps_legacy_key_job_id_parsing():
    assert worker._job_id_from_key("jobs/job-old/document.pdf") == "job-old"
    assert worker._job_id_from_key("users/user-1/jobs/job-new/document.pdf") == "job-new"


def test_worker_skips_event_when_job_state_prevents_queued_transition(monkeypatch):
    class UnusedS3:
        def head_object(self, **_kwargs):
            raise AssertionError("A job marked for deletion must not start processing")

    monkeypatch.setattr(worker, "s3_client", UnusedS3())
    monkeypatch.setattr(worker, "_update_job", lambda *_args, **_kwargs: False)
    key = "users/user-sub/jobs/job-deleting/report.pdf"
    record = {
        "messageId": "message-deleting",
        "attributes": {"ApproximateReceiveCount": "1"},
        "body": json.dumps({"Records": [{"s3": {"bucket": {"name": "documents"}, "object": {"key": key}}}]}),
    }

    worker._process_record(record)
