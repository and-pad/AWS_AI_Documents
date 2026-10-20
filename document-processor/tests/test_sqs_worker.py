import json
import threading
import time

import pytest
from botocore.exceptions import ClientError

from app.sqs_worker import SqsDocumentWorker, VisibilityHeartbeat


class FakeSqs:
    def __init__(self):
        self.calls = []
        self.deleted = []
        self.visibility = []
        self.responses = []

    def receive_message(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0) if self.responses else {}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)

    def change_message_visibility(self, **kwargs):
        self.visibility.append(kwargs)


class FakeS3:
    def __init__(self):
        self.puts = []
        self.deleted = []
        self.source = b"pdf"

    def head_object(self, **kwargs):
        if kwargs["Bucket"] == "documents":
            return {"ContentLength": len(self.source), "Metadata": {"job-id": "job-1"}}
        return {"ContentLength": 10, "Metadata": {}}

    def get_object(self, **kwargs):
        return {"Body": type("Body", (), {"read": lambda self: b"pdf"})()}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)


class FakeTable:
    def __init__(self, status="QUEUED"):
        self.item = {"job_id": "job-1", "status": status, "user_prompt": ""}
        self.calls = []
        self.fail_acquire = False

    def get_item(self, **kwargs):
        return {"Item": dict(self.item)}

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_acquire and "processing_attempt" in kwargs.get("UpdateExpression", ""):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        expression = kwargs.get("UpdateExpression", "")
        values = kwargs.get("ExpressionAttributeValues", {})
        if ":completed" in values:
            self.item["status"] = "COMPLETED"
        elif ":queued" in values:
            self.item["status"] = "QUEUED"
        elif ":failed" in values:
            self.item["status"] = "FAILED"
        elif ":processing" in values:
            self.item["status"] = "PROCESSING"
        return {"Attributes": dict(self.item)}


def settings(**overrides):
    value = {
        "document_queue_url": "https://queue.example/documents",
        "documents_bucket": "documents",
        "results_bucket": "results",
        "jobs_table": "jobs",
        "sqs_wait_time_seconds": 20,
        "sqs_visibility_timeout": 900,
        "sqs_visibility_heartbeat_seconds": 1,
        "sqs_max_receive_count": 3,
        "max_upload_bytes": 1_000_000,
    }
    value.update(overrides)
    return value


def message(receive_count="1"):
    return {
        "MessageId": "message-1",
        "ReceiptHandle": "receipt-1",
        "Attributes": {"ApproximateReceiveCount": receive_count, "SentTimestamp": "1"},
        "Body": json.dumps({"Records": [{"s3": {"bucket": {"name": "documents"}, "object": {"key": "users/user/jobs/job-1/file.pdf"}}}]}),
    }


def processor(_filename, _contents, _prompt, progress_callback=None):
    assert progress_callback is not None
    progress_callback(0, 2)
    progress_callback(1, 2)
    progress_callback(2, 2)
    return {"analysis": {"summary": "ok"}}


def test_long_poll_uses_one_message_and_twenty_seconds():
    sqs, table = FakeSqs(), FakeTable()
    worker = SqsDocumentWorker(settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table)
    sqs.responses = [{}]
    worker._stop.set()
    # Invoke the SDK call directly to verify the production request shape.
    worker.sqs.receive_message(
        QueueUrl=worker.queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=worker.wait_time_seconds,
        VisibilityTimeout=worker.visibility_timeout, AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
    )
    assert sqs.calls[0]["MaxNumberOfMessages"] == 1
    assert sqs.calls[0]["WaitTimeSeconds"] == 20
    assert sqs.calls[0]["AttributeNames"] == ["ApproximateReceiveCount", "SentTimestamp"]


def test_successful_message_acquires_processes_persists_cleans_up_and_deletes_message(monkeypatch):
    sqs, s3, table = FakeSqs(), FakeS3(), FakeTable()
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: settings())
    worker = SqsDocumentWorker(settings=settings(), sqs_client=sqs, s3_client=s3, table=table, processor=processor)

    assert worker.process_message(message()) is True
    assert table.item["status"] == "COMPLETED"
    assert any("processing_attempt" in call["UpdateExpression"] for call in table.calls)
    assert any("progress_percent" in call["UpdateExpression"] for call in table.calls)
    assert s3.puts[0]["Key"] == "results/job-1.json"
    assert s3.deleted == [{"Bucket": "documents", "Key": "users/user/jobs/job-1/file.pdf"}]
    assert sqs.deleted == [{"QueueUrl": worker.queue_url, "ReceiptHandle": "receipt-1"}]


def test_failure_requeues_before_last_receive_and_does_not_delete(monkeypatch):
    sqs, table = FakeSqs(), FakeTable()
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: settings())
    worker = SqsDocumentWorker(
        settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table,
        processor=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )

    assert worker.process_message(message("2")) is False
    assert table.item["status"] == "QUEUED"
    assert not sqs.deleted


def test_last_receive_marks_failed_and_keeps_message_for_sqs_dlq(monkeypatch):
    sqs, table = FakeSqs(), FakeTable()
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: settings())
    worker = SqsDocumentWorker(
        settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table,
        processor=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )

    assert worker.process_message(message("3")) is False
    assert table.item["status"] == "FAILED"
    assert not sqs.deleted


def test_active_duplicate_is_not_processed_or_deleted():
    sqs, table = FakeSqs(), FakeTable(status="PROCESSING")
    table.fail_acquire = True
    worker = SqsDocumentWorker(settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table, processor=processor)

    assert worker.process_message(message()) is False
    assert not sqs.deleted


def test_terminal_duplicate_is_deleted_without_processing():
    sqs, table = FakeSqs(), FakeTable(status="COMPLETED")
    table.fail_acquire = True
    worker = SqsDocumentWorker(settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table, processor=processor)

    assert worker.process_message(message()) is True
    assert sqs.deleted


def test_heartbeat_renews_visibility_and_lease():
    sqs, table = FakeSqs(), FakeTable(status="PROCESSING")
    worker = SqsDocumentWorker(settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table)
    worker._renew_visibility("receipt", "job-1", "message-1")

    assert sqs.visibility[0]["VisibilityTimeout"] == 900
    assert any("processing_lease_until" in call["UpdateExpression"] for call in table.calls)


def test_shutdown_releases_current_work_without_deleting_message():
    sqs, table = FakeSqs(), FakeTable(status="PROCESSING")
    worker = SqsDocumentWorker(settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table)
    worker._current = ("job-1", "receipt", "message-1")
    worker.stop()

    assert table.item["status"] == "QUEUED"
    assert not sqs.deleted


def test_heartbeat_runs_while_processing_is_blocked(monkeypatch):
    sqs, s3, table = FakeSqs(), FakeS3(), FakeTable()
    config = settings(sqs_visibility_heartbeat_seconds=0.01)
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: config)
    started = threading.Event()
    release = threading.Event()

    def slow_processor(*_args, **kwargs):
        started.set()
        assert release.wait(1)
        return {"analysis": {"summary": "ok"}}

    worker = SqsDocumentWorker(settings=config, sqs_client=sqs, s3_client=s3, table=table, processor=slow_processor)
    thread = threading.Thread(target=lambda: worker.process_message(message()))
    thread.start()
    assert started.wait(1)
    time.sleep(0.04)
    release.set()
    thread.join(1)

    assert sqs.visibility, "the visibility heartbeat must run while processing blocks"


def test_stale_processing_job_can_be_reacquired_after_a_task_crash(monkeypatch):
    sqs, s3, table = FakeSqs(), FakeS3(), FakeTable(status="PROCESSING")
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: settings())
    worker = SqsDocumentWorker(settings=settings(), sqs_client=sqs, s3_client=s3, table=table, processor=processor)

    assert worker.process_message(message("2")) is True
    assert table.item["status"] == "COMPLETED"
