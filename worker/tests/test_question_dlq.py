import json
import time

from botocore.exceptions import ClientError

from app import question_worker as worker


class QuestionTable:
    def __init__(self, item=None, error=None):
        self.item = dict(item or {})
        self.error = error
        self.calls = []

    def get_item(self, *, Key, **_kwargs):
        if self.item.get("job_id") == Key["job_id"] and self.item.get("ask_id") == Key["ask_id"]:
            return {"Item": dict(self.item)}
        return {}

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        if (
            not self.item
            or self.item.get("user_id") != values[":owner"]
            or self.item.get("expires_at", 0) <= values[":now"]
            or self.item.get("status") not in {values[":pending"], values[":processing"]}
        ):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.item.update({
            "status": values[":failed"], "error": values[":error"],
            "error_category": values[":category"], "failed_at": values[":now_iso"],
            "updated_at": values[":now_iso"],
        })
        self.item.pop("processing_owner", None)
        self.item.pop("processing_lease_until", None)


def dlq_record(*, job_id="job-a", ask_id="ask-a", user_id="user-a", message_id="dlq-1"):
    return {
        "messageId": message_id,
        "body": json.dumps({"job_id": job_id, "ask_id": ask_id, "user_id": user_id}),
    }


def processing_item(**overrides):
    return {
        "job_id": "job-a", "ask_id": "ask-a", "user_id": "user-a", "status": "PROCESSING",
        "expires_at": int(time.time()) + 3600, "processing_owner": "old-owner",
        "processing_lease_until": int(time.time()) + 500, **overrides,
    }


def test_dlq_marks_abruptly_terminated_processing_ask_failed(monkeypatch):
    table = QuestionTable(processing_item(processing_lease_until=int(time.time()) - 1))
    monkeypatch.setattr(worker, "_table", lambda _name: table)

    response = worker.lambda_dlq_handler({"Records": [dlq_record()]}, None)

    assert response == {"batchItemFailures": []}
    assert table.item["status"] == "FAILED"
    assert table.item["error_category"] == "retry_exhausted"
    assert "processing_owner" not in table.item
    assert "answer" not in table.item


def test_dlq_does_not_overwrite_completed_or_deleted_ask(monkeypatch):
    completed = QuestionTable(processing_item(status="COMPLETED", answer="already done"))
    monkeypatch.setattr(worker, "_table", lambda _name: completed)
    assert worker._process_dlq_message(dlq_record()) is True
    assert completed.item["status"] == "COMPLETED"
    assert completed.item["answer"] == "already done"

    deleted = QuestionTable()
    monkeypatch.setattr(worker, "_table", lambda _name: deleted)
    assert worker._process_dlq_message(dlq_record()) is True


def test_dlq_retries_transient_dynamodb_failure_with_partial_batch_response(monkeypatch):
    transient = ClientError(
        {"Error": {"Code": "InternalServerError"}, "ResponseMetadata": {"HTTPStatusCode": 500}},
        "UpdateItem",
    )
    table = QuestionTable(processing_item(), error=transient)
    monkeypatch.setattr(worker, "_table", lambda _name: table)

    response = worker.lambda_dlq_handler({"Records": [dlq_record(message_id="dlq-retry")]}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "dlq-retry"}]}


def test_dlq_does_not_fail_a_duplicate_while_another_lambda_holds_the_lease(monkeypatch):
    table = QuestionTable(processing_item())
    changes = []
    monkeypatch.setattr(worker, "_table", lambda _name: table)
    monkeypatch.setattr(worker.sqs_client, "change_message_visibility", lambda **kwargs: changes.append(kwargs))
    monkeypatch.setenv("QUESTION_DLQ_URL", "https://sqs.example.test/questions-dlq")
    record = dlq_record()
    record["receiptHandle"] = "dlq-receipt"

    assert worker._process_dlq_message(record) is False
    assert table.item["status"] == "PROCESSING"
    assert table.calls == []
    assert changes == [{
        "QueueUrl": "https://sqs.example.test/questions-dlq",
        "ReceiptHandle": "dlq-receipt",
        "VisibilityTimeout": 60,
    }]


def test_dlq_acknowledges_malformed_message_without_logging_document_content(monkeypatch, caplog):
    monkeypatch.setattr(worker, "_table", lambda _name: (_ for _ in ()).throw(AssertionError("must not access table")))
    secret_question = "private document question"
    response = worker.lambda_dlq_handler({"Records": [{
        "messageId": "bad", "body": json.dumps({"question": secret_question}),
    }]}, None)
    assert response == {"batchItemFailures": []}
    assert secret_question not in caplog.text
