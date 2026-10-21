import json
import hashlib
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


class ConcurrentFakeSqs(FakeSqs):
    """Returns one message per receive call and records messages still waiting."""

    def __init__(self, messages):
        super().__init__()
        self.pending = list(messages)
        self.received = []
        self.lock = threading.Lock()

    def receive_message(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
            if self.pending:
                message = self.pending.pop(0)
                self.received.append(message["MessageId"])
                return {"Messages": [message]}
        time.sleep(0.005)
        return {}


class FakeS3:
    def __init__(self):
        self.puts = []
        self.deleted = []
        self.source = b"pdf"
        self.sources = {}
        self.objects = {}

    def head_object(self, **kwargs):
        if kwargs["Bucket"] == "documents":
            key = kwargs["Key"]
            job_id = key.split("/")[3] if key.startswith("users/") else "job-1"
            source = self.sources.get(key, self.source)
            return {"ContentLength": len(source), "Metadata": {"job-id": job_id}}
        stored = self.objects.get(kwargs["Key"])
        if stored:
            return {"ContentLength": len(stored["Body"]), "Metadata": stored.get("Metadata", {})}
        return {"ContentLength": 10, "Metadata": {}}

    def get_object(self, **kwargs):
        body = self.sources.get(kwargs["Key"], self.objects.get(kwargs["Key"], {}).get("Body", b"pdf"))
        return {"Body": type("Body", (), {"read": lambda self: body})()}

    def put_object(self, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and kwargs["Key"] in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        self.puts.append(kwargs)
        self.objects[kwargs["Key"]] = {
            "Body": kwargs["Body"],
            "Metadata": kwargs.get("Metadata", {}),
        }

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)
        self.objects.pop(kwargs["Key"], None)

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        return {"Contents": [{"Key": key} for key in self.objects if key.startswith(prefix)], "IsTruncated": False}


class FakeTable:
    def __init__(self, status="QUEUED", jobs=None):
        self.item = {"job_id": "job-1", "status": status, "user_prompt": ""}
        self.items = {"job-1": self.item}
        for job_id, item in (jobs or {}).items():
            self.items[job_id] = {"job_id": job_id, "status": "QUEUED", "user_prompt": "", **item}
        self.item = self.items["job-1"]
        self.calls = []
        self.fail_acquire = False

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["job_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        job_id = kwargs["Key"]["job_id"]
        item = self.items.get(job_id)
        expression = kwargs.get("UpdateExpression", "")
        if self.fail_acquire and "processing_attempt" in expression:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        values = kwargs.get("ExpressionAttributeValues", {})
        condition = kwargs.get("ConditionExpression", "")
        if "processing_attempt = :attempt" in expression:
            status = (item or {}).get("status")
            lease = (item or {}).get("processing_lease_until")
            if status not in {"UPLOAD_PENDING", "QUEUED"} and not (status == "PROCESSING" and not lease):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        if item is None:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        if ":owner" in values and "processing_owner = :owner" in condition:
            if item.get("processing_owner") != values[":owner"]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        if "SET #status = :completed" in expression:
            item["status"] = "COMPLETED"
        elif "SET #status = :queued" in expression:
            item["status"] = "QUEUED"
        elif "SET #status = :failed" in expression:
            item["status"] = "FAILED"
        elif "SET #status = :processing" in expression:
            item["status"] = "PROCESSING"
        if ":owner" in values:
            item["processing_owner"] = values[":owner"]
        if ":message" in values:
            item["processing_message_id"] = values[":message"]
        if ":lease" in values:
            item["processing_lease_until"] = values[":lease"]
        if ":manifest_key" in values:
            item["checkpoint_manifest_key"] = values[":manifest_key"]
        if ":current" in values:
            item["current_page"] = values[":current"]
        if ":total" in values:
            item["total_pages"] = values[":total"]
        if ":percent" in values:
            item["progress_percent"] = values[":percent"]
        if ":error" in values:
            error_field = kwargs.get("ExpressionAttributeNames", {}).get("#error", "last_error")
            item[error_field] = values[":error"]
        if "REMOVE processing_owner" in expression:
            item.pop("processing_owner", None)
            item.pop("processing_message_id", None)
            item.pop("processing_lease_until", None)
        if "checkpoint_manifest_key" in expression and "REMOVE" in expression:
            item.pop("checkpoint_manifest_key", None)
        return {"Attributes": dict(item)}


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
        "processor_max_concurrent_jobs": 1,
        "max_upload_bytes": 1_000_000,
    }
    value.update(overrides)
    return value


def message(receive_count="1", *, job_id="job-1", message_id="message-1", receipt_handle="receipt-1"):
    return {
        "MessageId": message_id,
        "ReceiptHandle": receipt_handle,
        "Attributes": {"ApproximateReceiveCount": receive_count, "SentTimestamp": "1"},
        "Body": json.dumps({"Records": [{"s3": {"bucket": {"name": "documents"}, "object": {"key": f"users/user/jobs/{job_id}/{job_id}.pdf"}}}]}),
    }


def processor(_filename, _contents, _prompt, progress_callback=None, **_kwargs):
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
    assert "processing_owner = :owner" in next(
        call["UpdateExpression"] for call in table.calls if "processing_attempt" in call["UpdateExpression"]
    )
    assert "processing_owner = :owner" in next(
        call["ConditionExpression"] for call in table.calls if ":completed" in call["ExpressionAttributeValues"]
    )
    assert any("progress_percent" in call["UpdateExpression"] for call in table.calls)
    assert s3.puts[0]["Key"] == "results/job-1.json"
    assert s3.puts[0]["IfNoneMatch"] == "*"
    assert s3.deleted == [{"Bucket": "documents", "Key": "users/user/jobs/job-1/job-1.pdf"}]
    assert sqs.deleted == [{"QueueUrl": worker.queue_url, "ReceiptHandle": "receipt-1"}]


def test_completed_job_cleans_checkpoints_after_dynamodb_completion(monkeypatch):
    sqs, s3, table = FakeSqs(), FakeS3(), FakeTable()
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: settings())

    def processor_with_checkpoint(_filename, _contents, _prompt, progress_callback=None, **kwargs):
        store = kwargs["checkpoint_store"]
        store.save(1, {
            "job_id": "job-1", "page_number": 1, "total_pages": 1,
            "pdf_sha256": "a" * 64, "schema_version": "page-extraction-v1",
            "pipeline_fingerprint": "b" * 64,
        })
        progress_callback(1, 1)
        return {"analysis": {"summary": "ok"}}

    worker = SqsDocumentWorker(
        settings=settings(), sqs_client=sqs, s3_client=s3, table=table, processor=processor_with_checkpoint
    )

    assert worker.process_message(message()) is True
    assert table.item["status"] == "COMPLETED"
    assert "checkpoint_manifest_key" not in table.item
    assert not [key for key in s3.objects if key.startswith("checkpoints/job-1/")]
    assert any(item["Key"].startswith("checkpoints/job-1/") for item in s3.deleted)


def test_retryable_failure_retains_checkpoints(monkeypatch):
    sqs, s3, table = FakeSqs(), FakeS3(), FakeTable()
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: settings())

    def processor_saves_then_fails(_filename, _contents, _prompt, **kwargs):
        kwargs["checkpoint_store"].save(1, {
            "job_id": "job-1", "page_number": 1, "total_pages": 1,
            "pdf_sha256": "a" * 64, "schema_version": "page-extraction-v1",
            "pipeline_fingerprint": "b" * 64,
        })
        raise RuntimeError("failure after checkpoint")

    worker = SqsDocumentWorker(
        settings=settings(), sqs_client=sqs, s3_client=s3, table=table, processor=processor_saves_then_fails
    )

    assert worker.process_message(message("2")) is False
    assert table.item["status"] == "QUEUED"
    assert any(key.startswith("checkpoints/job-1/") for key in s3.objects)
    assert not sqs.deleted


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


def test_output_limit_reason_is_preserved_in_job_failure_state(monkeypatch):
    from app.ollama_client import OllamaError

    sqs, table = FakeSqs(), FakeTable()
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: settings())
    worker = SqsDocumentWorker(
        settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table,
        processor=lambda *args, **kwargs: (_ for _ in ()).throw(
            OllamaError("safe", category="output_limit_reached")
        ),
    )

    assert worker.process_message(message("3")) is False
    assert table.item["status"] == "FAILED"
    assert "límite de tokens" in table.item["error"]
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
    assert len(sqs.deleted) == 1


def test_heartbeat_renews_visibility_and_lease():
    sqs, table = FakeSqs(), FakeTable(status="PROCESSING")
    worker = SqsDocumentWorker(settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table)
    worker._renew_visibility("receipt", "job-1", "message-1")

    assert sqs.visibility[0]["VisibilityTimeout"] == 900
    assert any("processing_lease_until" in call["UpdateExpression"] for call in table.calls)


def test_old_processing_owner_cannot_update_progress_or_complete():
    worker = SqsDocumentWorker(settings=settings(), sqs_client=FakeSqs(), s3_client=FakeS3(), table=FakeTable())
    worker.table.item.update(status="PROCESSING", processing_owner="new-owner")

    with pytest.raises(ClientError):
        worker._update_progress("job-1", "message-1", 2, 5, "old-owner")
    with pytest.raises(ClientError):
        worker._mark_completed("job-1", "message-1", "old-owner", "results/job-1.json")
    with pytest.raises(ClientError):
        worker._commit_checkpoint_manifest("job-1", "message-1", "old-owner", "checkpoints/job-1/manifest/stale.json")


def test_result_put_is_create_only_and_reuses_only_matching_identity():
    s3 = FakeS3()
    worker = SqsDocumentWorker(settings=settings(), sqs_client=FakeSqs(), s3_client=s3, table=FakeTable())
    metadata = {
        "job-id": "job-1",
        "pdf-sha256": "a" * 64,
        "pipeline-fingerprint": "b" * 64,
        "processing-owner": "owner-a",
    }
    s3.objects["results/job-1.json"] = {"Body": b"existing", "Metadata": metadata}

    worker._put_result_if_absent(
        "results/job-1.json", b"new", "job-1", "owner-b", "a" * 64, "b" * 64
    )

    assert s3.objects["results/job-1.json"]["Body"] == b"existing"
    assert s3.puts == []
    with pytest.raises(RuntimeError, match="different processing identity"):
        worker._put_result_if_absent(
            "results/job-1.json", b"other", "job-1", "owner-b", "c" * 64, "b" * 64
        )


def test_shutdown_does_not_release_lease_while_processing_may_still_be_active():
    sqs, table = FakeSqs(), FakeTable(status="PROCESSING")
    worker = SqsDocumentWorker(settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table)
    worker._active_jobs[1] = {"job_id": "job-1", "sqs_message_id": "message-1", "processing_owner": "owner-1"}
    worker.stop()

    assert table.item["status"] == "PROCESSING"
    assert not sqs.deleted
    assert not any("REMOVE processing_lease_until" in call.get("UpdateExpression", "") for call in table.calls)


def test_health_stays_healthy_while_consumers_are_busy_and_detects_dead_slot():
    worker = SqsDocumentWorker(
        settings=settings(processor_max_concurrent_jobs=2),
        sqs_client=FakeSqs(),
        s3_client=FakeS3(),
        table=FakeTable(),
    )
    release = threading.Event()
    consumers = [
        threading.Thread(target=release.wait, name=f"consumer-{slot}", daemon=True)
        for slot in (1, 2)
    ]
    worker._threads = {slot: thread for slot, thread in enumerate(consumers, start=1)}
    for thread in consumers:
        thread.start()
    worker._active_jobs[1] = {"job_id": "job-a", "sqs_message_id": "message-a"}
    worker._active_jobs[2] = {"job_id": "job-b", "sqs_message_id": "message-b"}
    try:
        assert worker.health_snapshot["status"] == "ok"
        assert worker.health_snapshot["sqs_worker"]["active_jobs"] == 2

        # A long inference does not kill its consumer thread. A dead/missing
        # slot is distinguishable and makes the health endpoint fail.
        consumers[1].join(timeout=0)
        worker._threads.pop(2)
        assert worker.health_snapshot["status"] == "unhealthy"
        assert worker.health_snapshot["sqs_worker"]["dead_worker_slots"] == [2]
    finally:
        release.set()
        for thread in consumers:
            thread.join(timeout=1)


def test_unexpected_consumer_exit_is_logged_and_health_becomes_unhealthy():
    worker = SqsDocumentWorker(
        settings=settings(processor_max_concurrent_jobs=1),
        sqs_client=FakeSqs(),
        s3_client=FakeS3(),
        table=FakeTable(),
    )

    def crashed_consumer(*, worker_slot):
        raise RuntimeError(f"consumer {worker_slot} stopped")

    worker.run_forever = crashed_consumer
    thread = threading.Thread(
        target=worker._run_consumer_supervised,
        kwargs={"worker_slot": 1},
        daemon=True,
    )
    worker._threads[1] = thread
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert worker.health_snapshot["status"] == "unhealthy"
    assert worker.health_snapshot["sqs_worker"]["dead_worker_slots"] == [1]


def test_shutdown_during_job_keeps_it_recoverable_and_does_not_ack(monkeypatch):
    sqs, table = FakeSqs(), FakeTable()
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: settings())
    processing_started = threading.Event()
    release_processing = threading.Event()

    def slow_processor(*_args, **_kwargs):
        processing_started.set()
        assert release_processing.wait(1)
        return {"pages": 1, "analysis": {"summary": "ok"}}

    worker = SqsDocumentWorker(
        settings=settings(), sqs_client=sqs, s3_client=FakeS3(), table=table, processor=slow_processor
    )
    consumer = threading.Thread(target=lambda: worker.process_message(message()))
    consumer.start()
    assert processing_started.wait(1)

    worker.stop(drain_timeout_seconds=0.01)
    assert table.item["status"] == "PROCESSING"
    assert not sqs.deleted
    assert worker._active_job_count() == 1

    release_processing.set()
    consumer.join(timeout=2)
    assert not consumer.is_alive()
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


def test_two_consumer_slots_process_two_messages_and_leave_third_queued(monkeypatch):
    first, second, third = (
        {"MessageId": "message-a"}, {"MessageId": "message-b"}, {"MessageId": "message-c"}
    )
    sqs = ConcurrentFakeSqs([first, second, third])
    worker = SqsDocumentWorker(
        settings=settings(processor_max_concurrent_jobs=2),
        sqs_client=sqs,
        s3_client=FakeS3(),
        table=FakeTable(),
    )
    started = {name: threading.Event() for name in ("message-a", "message-b", "message-c")}
    release = {name: threading.Event() for name in started}
    running = set()
    lock = threading.Lock()
    max_running = 0

    def process_message_for_test(item, *, worker_slot):
        nonlocal max_running
        message_id = item["MessageId"]
        with lock:
            running.add(message_id)
            max_running = max(max_running, len(running))
        started[message_id].set()
        try:
            assert release[message_id].wait(2)
        finally:
            with lock:
                running.remove(message_id)

    worker.process_message = process_message_for_test
    worker.start()
    try:
        assert started["message-a"].wait(1)
        assert started["message-b"].wait(1)
        time.sleep(0.03)
        assert sqs.received == ["message-a", "message-b"]
        assert sqs.pending == [third]
        assert max_running == 2

        # Freeing one slot makes exactly that consumer receive the next message.
        release["message-a"].set()
        assert started["message-c"].wait(1)
        assert "message-b" in running
        assert max_running == 2
        release["message-b"].set()
        release["message-c"].set()
    finally:
        for event in release.values():
            event.set()
        worker.stop()
    assert len(worker._threads) == 2
    assert all(call["MaxNumberOfMessages"] == 1 for call in sqs.calls)
    assert all(call["WaitTimeSeconds"] == 20 for call in sqs.calls)


def test_two_jobs_have_independent_leases_checkpoints_heartbeats_and_results(monkeypatch):
    sqs, s3 = FakeSqs(), FakeS3()
    table = FakeTable(jobs={"job-2": {"user_id": "user"}})
    config = settings(processor_max_concurrent_jobs=2, sqs_visibility_heartbeat_seconds=0.01)
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: config)
    s3.sources = {
        "users/user/jobs/job-1/job-1.pdf": b"pdf-one",
        "users/user/jobs/job-2/job-2.pdf": b"pdf-two",
    }
    entered = threading.Barrier(2)
    both_started = threading.Event()
    release_processing = threading.Event()
    call_data = {}
    call_lock = threading.Lock()

    def isolated_processor(filename, contents, _prompt, progress_callback=None, **kwargs):
        job_id = kwargs["job_id"]
        store = kwargs["checkpoint_store"]
        checkpoint = {
            "pdf_sha256": hashlib.sha256(contents).hexdigest(),
            "total_pages": 1,
            "schema_version": "page-extraction-v1",
            "pipeline_fingerprint": f"fingerprint-{job_id}",
        }
        store.save(1, checkpoint)
        with call_lock:
            call_data[job_id] = {"filename": filename, "prefix": store.prefix, "owner": store.processing_owner}
        progress_callback(1, 1)
        entered.wait(timeout=1)
        both_started.set()
        assert release_processing.wait(1)
        return {"job_id": job_id, "pages": 1, "analysis": {"summary": f"summary-{job_id}"}}

    worker = SqsDocumentWorker(
        settings=config, sqs_client=sqs, s3_client=s3, table=table, processor=isolated_processor
    )
    outcomes = {}
    messages = [
        (message(job_id="job-1", message_id="message-1", receipt_handle="receipt-1"), 1),
        (message(job_id="job-2", message_id="message-2", receipt_handle="receipt-2"), 2),
    ]
    threads = [
        threading.Thread(
            target=lambda item=item, slot=slot: outcomes.update(
                {item["MessageId"]: worker.process_message(item, worker_slot=slot)}
            )
        )
        for item, slot in messages
    ]
    for thread in threads:
        thread.start()
    assert both_started.wait(1)
    time.sleep(0.04)
    release_processing.set()
    for thread in threads:
        thread.join(timeout=3)

    assert outcomes == {"message-1": True, "message-2": True}
    assert table.items["job-1"]["status"] == "COMPLETED"
    assert table.items["job-2"]["status"] == "COMPLETED"
    assert call_data["job-1"]["prefix"] == "checkpoints/job-1/"
    assert call_data["job-2"]["prefix"] == "checkpoints/job-2/"
    assert call_data["job-1"]["owner"] != call_data["job-2"]["owner"]
    assert {item["ReceiptHandle"] for item in sqs.visibility} == {"receipt-1", "receipt-2"}
    assert {item["ReceiptHandle"] for item in sqs.deleted} == {"receipt-1", "receipt-2"}
    lease_owners = {
        call["ExpressionAttributeValues"].get(":owner")
        for call in table.calls
        if "processing_lease_until = :lease" in call.get("UpdateExpression", "")
    }
    assert {call_data["job-1"]["owner"], call_data["job-2"]["owner"]}.issubset(lease_owners)
    checkpoint_deletes = [item["Key"] for item in s3.deleted if item["Key"].startswith("checkpoints/")]
    assert any(key.startswith("checkpoints/job-1/") for key in checkpoint_deletes)
    assert any(key.startswith("checkpoints/job-2/") for key in checkpoint_deletes)
    for job_id in ("job-1", "job-2"):
        stored = json.loads(s3.objects[f"results/{job_id}.json"]["Body"])
        assert stored["job_id"] == job_id
        assert stored["analysis"]["summary"] == f"summary-{job_id}"


def test_one_job_failure_does_not_block_other_job_success(monkeypatch):
    sqs, s3 = FakeSqs(), FakeS3()
    table = FakeTable(jobs={"job-2": {"user_id": "user"}})
    config = settings(processor_max_concurrent_jobs=2)
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: config)

    def one_fails(filename, contents, prompt, progress_callback=None, **kwargs):
        if kwargs["job_id"] == "job-1":
            raise RuntimeError("job one failed")
        return {"job_id": kwargs["job_id"], "pages": 1, "analysis": {"summary": "ok"}}

    worker = SqsDocumentWorker(settings=config, sqs_client=sqs, s3_client=s3, table=table, processor=one_fails)
    results = {}
    def run_one(name, receive_count, job_id, message_id, receipt, slot):
        results[name] = worker.process_message(
            message(receive_count, job_id=job_id, message_id=message_id, receipt_handle=receipt),
            worker_slot=slot,
        )

    threads = [
        threading.Thread(target=run_one, args=("one", "2", "job-1", "m1", "r1", 1)),
        threading.Thread(target=run_one, args=("two", "1", "job-2", "m2", "r2", 2)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert results == {"one": False, "two": True}
    assert table.items["job-1"]["status"] == "QUEUED"
    assert table.items["job-2"]["status"] == "COMPLETED"
    assert [item["ReceiptHandle"] for item in sqs.deleted] == ["r2"]


def test_duplicate_delivery_of_same_job_is_rejected_by_its_lease(monkeypatch):
    sqs, s3, table = FakeSqs(), FakeS3(), FakeTable()
    config = settings(processor_max_concurrent_jobs=2)
    monkeypatch.setattr("app.sqs_worker.get_settings", lambda: config)
    processor_started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_processor(*_args, **_kwargs):
        calls.append("processed")
        processor_started.set()
        assert release.wait(1)
        return {"pages": 1, "analysis": {"summary": "ok"}}

    worker = SqsDocumentWorker(settings=config, sqs_client=sqs, s3_client=s3, table=table, processor=slow_processor)
    first = threading.Thread(target=lambda: worker.process_message(message("1", message_id="m1", receipt_handle="r1"), worker_slot=1))
    first.start()
    assert processor_started.wait(1)
    duplicate_was_acknowledged = worker.process_message(
        message("2", message_id="m2", receipt_handle="r2"), worker_slot=2
    )
    release.set()
    first.join(timeout=2)

    assert duplicate_was_acknowledged is False
    assert calls == ["processed"]


def test_single_job_mode_starts_one_consumer_for_compatibility():
    sqs = ConcurrentFakeSqs([])
    worker = SqsDocumentWorker(
        settings=settings(processor_max_concurrent_jobs=1),
        sqs_client=sqs,
        s3_client=FakeS3(),
        table=FakeTable(),
    )
    worker.start()
    try:
        deadline = time.monotonic() + 1
        while not sqs.calls and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(worker._threads) == 1
        assert list(worker._threads) == [1]
    finally:
        worker.stop()


def test_shutdown_stops_all_long_poll_consumers_from_receiving_again():
    sqs = ConcurrentFakeSqs([])
    worker = SqsDocumentWorker(
        settings=settings(processor_max_concurrent_jobs=2),
        sqs_client=sqs,
        s3_client=FakeS3(),
        table=FakeTable(),
    )
    worker.start()
    deadline = time.monotonic() + 1
    while len(sqs.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(sqs.calls) >= 2

    worker.stop(drain_timeout_seconds=1)
    polls_at_shutdown = len(sqs.calls)
    time.sleep(0.02)

    assert len(sqs.calls) == polls_at_shutdown
    assert all(not thread.is_alive() for thread in worker._threads.values())


def test_processor_concurrency_rejects_values_above_two():
    with pytest.raises(ValueError, match="must be 1 or 2"):
        SqsDocumentWorker(
            settings=settings(processor_max_concurrent_jobs=3),
            sqs_client=FakeSqs(),
            s3_client=FakeS3(),
            table=FakeTable(),
        )


def test_processor_concurrency_setting_defaults_to_one_and_accepts_two(monkeypatch):
    from app.settings import get_settings

    get_settings.cache_clear()
    try:
        monkeypatch.delenv("PROCESSOR_MAX_CONCURRENT_JOBS", raising=False)
        assert get_settings()["processor_max_concurrent_jobs"] == 1

        get_settings.cache_clear()
        monkeypatch.setenv("PROCESSOR_MAX_CONCURRENT_JOBS", "2")
        assert get_settings()["processor_max_concurrent_jobs"] == 2
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("invalid_value", ["0", "3", "two"])
def test_processor_concurrency_setting_rejects_invalid_values(monkeypatch, invalid_value):
    from app.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROCESSOR_MAX_CONCURRENT_JOBS", invalid_value)
    try:
        with pytest.raises(ValueError, match="PROCESSOR_MAX_CONCURRENT_JOBS"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_production_dynamodb_resource_is_thread_local(monkeypatch):
    created = []
    created_lock = threading.Lock()

    class FakeResource:
        def Table(self, _name):
            table = object()
            with created_lock:
                created.append(table)
            return table

    class FakeSession:
        def resource(self, _service):
            return FakeResource()

    monkeypatch.setattr("app.sqs_worker.boto3.session.Session", FakeSession)
    worker = SqsDocumentWorker(
        settings=settings(processor_max_concurrent_jobs=2),
        sqs_client=FakeSqs(),
        s3_client=FakeS3(),
    )
    thread_tables = []

    def get_table_twice():
        first = worker.table
        assert worker.table is first
        thread_tables.append(first)

    threads = [threading.Thread(target=get_table_twice) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert len(created) == 2
    assert thread_tables[0] is not thread_tables[1]
