import json
import logging

import pytest
from botocore.exceptions import ClientError

from app import question_worker as worker
from app.ollama_qa_client import QuestionModelError


class Table:
    name = "questions"


def record(*, receive_count=1, job_id="job-a", ask_id="ask-a", user_id="user-a"):
    return {
        "messageId": "sqs-message-1",
        "receiptHandle": "receipt-1",
        "attributes": {"ApproximateReceiveCount": str(receive_count)},
        "body": json.dumps({"job_id": job_id, "ask_id": ask_id, "user_id": user_id}),
    }


def active_ask(**overrides):
    return {
        "job_id": "job-a", "ask_id": "ask-a", "user_id": "user-a", "question": "¿Quién firmó?",
        "status": "PROCESSING", "processing_owner": "worker-1", **overrides,
    }


def setup(monkeypatch, *, ask=None, job=None):
    captured = {"terminal": [], "retry": [], "complete": [], "ollama": [], "s3": [], "stages": []}
    monkeypatch.setattr(worker, "_table", lambda name: Table() if name == "QUESTION_TABLE" else object())
    monkeypatch.setattr(worker, "_claim_ask", lambda _table, *_args: dict(ask or active_ask()))
    monkeypatch.setattr(worker, "_load_completed_job", lambda *_args: job or {
        "job_id": "job-a", "user_id": "user-a", "status": "COMPLETED", "result_key": "results/job-a.json",
    })
    def load_result(bucket, key):
        captured["s3"].append({"Bucket": bucket, "Key": key})
        return {
            "pages": 1,
            "page_extractions": [{"page": 1, "transcription": "La firmante es Ana.", "analysis_notes": "Firma de Ana", "visual_elements": {}}],
        }
    monkeypatch.setattr(worker, "_load_result", load_result)
    monkeypatch.setattr(worker, "_token", lambda: "")
    monkeypatch.setattr(worker, "_complete_ask", lambda *args, **kwargs: captured["complete"].append((args, kwargs)) or True)
    monkeypatch.setattr(worker, "_set_terminal_error", lambda *args: captured["terminal"].append(args))
    monkeypatch.setattr(worker, "_release_for_retry", lambda *args: captured["retry"].append(args) or True)
    monkeypatch.setattr(worker, "_set_processing_stage", lambda *args: captured["stages"].append(args[-1]) or True)
    monkeypatch.setattr(worker, "ask_ollama", lambda *args, **kwargs: captured["ollama"].append((args, kwargs)) or {"answer": "Ana", "source_pages": [1]})
    monkeypatch.setenv("RESULTS_BUCKET", "private-results")
    monkeypatch.setenv("OLLAMA_ASK_NUM_CTX", "8192")
    monkeypatch.setenv("QUESTION_QUEUE_URL", "https://sqs.example.test/questions")
    monkeypatch.setattr(worker.sqs_client, "change_message_visibility", lambda **_kwargs: None)
    return captured


def test_worker_reads_only_authorized_completed_result_and_persists_answer(monkeypatch):
    captured = setup(monkeypatch)
    assert worker._process_message(record()) is True
    assert captured["s3"] == [{"Bucket": "private-results", "Key": "results/job-a.json"}]
    assert len(captured["ollama"]) == 1
    assert captured["complete"][0][0][1:4] == ("job-a", "ask-a", "user-a")
    assert captured["stages"] == ["WAITING_MODEL", "SAVING_RESULT"]
    context = captured["ollama"][0][0][1]
    assert "La firmante es Ana." in context.text
    assert captured["ollama"][0][1]["request_id"]


def test_deleted_job_cannot_publish_answer_after_ollama_finishes(monkeypatch):
    captured = setup(monkeypatch)
    monkeypatch.setattr(worker, "_complete_ask", lambda *_args, **_kwargs: False)
    assert worker._process_message(record()) is True
    assert len(captured["ollama"]) == 1
    assert len(captured["complete"]) == 0
    # The completion transaction in production includes an atomic parent-job
    # ConditionCheck; this simulated conflict represents DELETE setting DELETING.


def test_duplicate_delivery_of_terminal_ask_is_acknowledged_without_ollama(monkeypatch):
    captured = setup(monkeypatch)
    monkeypatch.setattr(worker, "_claim_ask", lambda *_args: None)
    assert worker._process_message(record()) is True
    assert captured["s3"] == []
    assert captured["ollama"] == []


def test_another_owner_in_queue_message_cannot_access_result(monkeypatch):
    captured = setup(monkeypatch, ask=active_ask(user_id="user-a"))
    assert worker._process_message(record(user_id="user-b")) is True
    assert captured["terminal"]
    assert captured["s3"] == []
    assert captured["ollama"] == []


def test_duplicate_delivery_waits_when_a_valid_processing_lease_is_owned(monkeypatch):
    captured = setup(monkeypatch)
    monkeypatch.setattr(worker, "_claim_ask", lambda *_args: (_ for _ in ()).throw(worker.AskLeaseBusy()))
    assert worker._process_message(record(receive_count=2)) is False
    assert captured["s3"] == []
    assert captured["ollama"] == []


def test_temporary_dynamodb_claim_failure_returns_message_for_retry(monkeypatch):
    captured = setup(monkeypatch)
    failure = ClientError(
        {"Error": {"Code": "InternalServerError"}, "ResponseMetadata": {"HTTPStatusCode": 500}},
        "UpdateItem",
    )
    monkeypatch.setattr(worker, "_claim_ask", lambda *_args: (_ for _ in ()).throw(failure))
    assert worker._process_message(record(receive_count=1)) is False
    assert not captured["terminal"]
    assert captured["ollama"] == []


@pytest.mark.parametrize("code", [
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "TransactionInProgressException",
])
def test_dynamodb_throttling_codes_with_http_400_are_retried(monkeypatch, code):
    captured = setup(monkeypatch)
    failure = ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        "UpdateItem",
    )
    monkeypatch.setattr(worker, "_claim_ask", lambda *_args: (_ for _ in ()).throw(failure))

    assert worker._process_message(record(receive_count=1)) is False
    assert not captured["terminal"]
    assert captured["ollama"] == []


def test_secrets_manager_temporary_failure_is_retried_without_logging_secret(monkeypatch, caplog):
    setup(monkeypatch)
    failure = ClientError(
        {"Error": {"Code": "InternalServerError"}, "ResponseMetadata": {"HTTPStatusCode": 500}},
        "GetSecretValue",
    )
    monkeypatch.setattr(worker, "_token", lambda: (_ for _ in ()).throw(failure))
    assert worker._process_message(record(receive_count=1)) is False
    assert "secret-token" not in caplog.text


def test_secret_token_is_cached_briefly_and_refreshed_after_ttl(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-2:123456789012:secret:ollama")
    monkeypatch.setattr(worker, "_cached_token", None)
    monkeypatch.setattr(worker, "_cached_token_expires_at", 0.0)
    clock = {"now": 100.0}
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock["now"])
    secrets = iter(["token-v1", "token-v2"])
    calls = []

    def get_secret_value(**kwargs):
        calls.append(kwargs)
        return {"SecretString": next(secrets)}

    monkeypatch.setattr(worker.secrets_client, "get_secret_value", get_secret_value)

    assert worker._token() == "token-v1"
    assert worker._token() == "token-v1"
    assert len(calls) == 1

    clock["now"] += worker._TOKEN_CACHE_TTL_SECONDS + 1
    assert worker._token() == "token-v2"
    assert len(calls) == 2
    assert calls[0] == {"SecretId": "arn:aws:secretsmanager:us-east-2:123456789012:secret:ollama"}


def test_configured_empty_secret_fails_closed_without_exposing_secret(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-2:123456789012:secret:ollama")
    monkeypatch.setattr(worker, "_cached_token", None)
    monkeypatch.setattr(worker, "_cached_token_expires_at", 0.0)
    monkeypatch.setattr(worker.secrets_client, "get_secret_value", lambda **_kwargs: {"SecretString": "  "})

    with pytest.raises(QuestionModelError) as failure:
        worker._token()

    assert failure.value.category == "missing_secret"
    assert failure.value.retryable is False
    assert "arn:aws" not in str(failure.value)


def test_missing_result_object_marks_ask_failed_without_calling_ollama(monkeypatch):
    captured = setup(monkeypatch)
    missing = ClientError(
        {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "GetObject",
    )
    monkeypatch.setattr(worker, "_load_result", lambda *_args: (_ for _ in ()).throw(missing))
    assert worker._process_message(record()) is True
    assert captured["terminal"]
    assert captured["ollama"] == []


def test_removed_or_noncompleted_job_is_not_sent_to_ollama(monkeypatch):
    captured = setup(monkeypatch)
    monkeypatch.setattr(worker, "_load_completed_job", lambda *_args: None)
    assert worker._process_message(record()) is True
    assert captured["terminal"]
    assert captured["s3"] == []
    assert captured["ollama"] == []


def test_transient_error_releases_claim_for_sqs_retry(monkeypatch):
    captured = setup(monkeypatch)
    monkeypatch.setattr(worker, "ask_ollama", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        QuestionModelError("read_timeout", "safe error", retryable=True)
    ))
    assert worker._process_message(record(receive_count=1)) is False
    assert captured["retry"]
    assert not captured["terminal"]


def test_output_budget_exhaustion_fails_terminally_without_identical_sqs_retry(monkeypatch):
    captured = setup(monkeypatch)
    monkeypatch.setattr(worker, "ask_ollama", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        QuestionModelError("output_budget_exhausted", "safe output limit message", retryable=False)
    ))

    assert worker._process_message(record(receive_count=1)) is True
    assert not captured["retry"]
    assert len(captured["terminal"]) == 1
    assert captured["terminal"][0][-1] == "output_budget_exhausted"
    assert not captured["complete"]


def test_transient_retry_uses_its_own_receipt_handle_and_short_backoff(monkeypatch):
    setup(monkeypatch)
    changes = []
    monkeypatch.setattr(worker.sqs_client, "change_message_visibility", lambda **kwargs: changes.append(kwargs))
    monkeypatch.setattr(worker, "ask_ollama", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        QuestionModelError("http_error", "safe error", retryable=True, http_status=502)
    ))

    assert worker._process_message(record(receive_count=2)) is False
    assert changes == [{
        "QueueUrl": "https://sqs.example.test/questions",
        "ReceiptHandle": "receipt-1",
        "VisibilityTimeout": 60,
    }]


def test_transaction_conflict_while_persisting_answer_is_retried(monkeypatch):
    captured = setup(monkeypatch)
    conflict = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "None"}, {"Code": "TransactionConflict"}],
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "TransactWriteItems",
    )
    monkeypatch.setattr(worker, "_complete_ask", lambda *_args, **_kwargs: (_ for _ in ()).throw(conflict))

    assert worker._process_message(record(receive_count=1)) is False
    assert captured["retry"]
    assert not captured["terminal"]


def test_unclassified_transaction_cancel_is_retried_safely(monkeypatch):
    captured = setup(monkeypatch)
    ambiguous_cancel = ClientError(
        {"Error": {"Code": "TransactionCanceledException"}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        "TransactWriteItems",
    )
    monkeypatch.setattr(worker, "_complete_ask", lambda *_args, **_kwargs: (_ for _ in ()).throw(ambiguous_cancel))

    assert worker._process_message(record(receive_count=1)) is False
    assert captured["retry"]
    assert not captured["terminal"]


def test_last_transient_failure_marks_ask_failed_and_leaves_message_for_dlq(monkeypatch):
    captured = setup(monkeypatch)
    monkeypatch.setattr(worker, "ask_ollama", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        QuestionModelError("read_timeout", "safe error", retryable=True)
    ))
    assert worker._process_message(record(receive_count=3)) is False
    assert captured["terminal"]
    assert not captured["retry"]


def test_last_transient_failure_releases_message_to_redrive_without_waiting_full_visibility(monkeypatch):
    setup(monkeypatch)
    changes = []
    monkeypatch.setattr(worker.sqs_client, "change_message_visibility", lambda **kwargs: changes.append(kwargs))
    monkeypatch.setattr(worker, "ask_ollama", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        QuestionModelError("read_timeout", "safe error", retryable=True)
    ))
    assert worker._process_message(record(receive_count=3)) is False
    assert changes[0]["VisibilityTimeout"] == 0
    assert changes[0]["ReceiptHandle"] == "receipt-1"


def test_result_error_does_not_log_document_question_or_token(monkeypatch, caplog):
    captured = setup(monkeypatch, ask=active_ask(question="private question"))
    monkeypatch.setattr(worker, "_load_result", lambda *_args: (_ for _ in ()).throw(ValueError("bad JSON")))
    with caplog.at_level(logging.INFO):
        assert worker._process_message(record()) is True
    assert "private question" not in caplog.text
    assert "La firmante" not in caplog.text
    assert "private-results" not in caplog.text
    assert captured["terminal"]


def test_terminal_failure_aliases_reserved_error_name_and_sets_terminal_stage():
    class Capture:
        name = "questions"

        def update_item(self, **kwargs):
            self.request = kwargs

    table = Capture()
    worker._set_terminal_error(table, "job-a", "ask-a", "owner", "safe message", "output_budget_exhausted")
    request = table.request
    assert "#error = :error" in request["UpdateExpression"]
    assert request["ExpressionAttributeNames"]["#error"] == "error"
    assert request["ExpressionAttributeValues"][":stage"] == "FAILED"
    assert request["ExpressionAttributeValues"][":failed"] == "FAILED"
    assert request["ExpressionAttributeValues"][":category"] == "output_budget_exhausted"
    assert request["ExpressionAttributeValues"][":error"] == "safe message"
    assert " error =" not in request["UpdateExpression"]


def test_retry_release_sets_retrying_stage():
    class Capture:
        name = "questions"

        def update_item(self, **kwargs):
            self.request = kwargs

    table = Capture()
    assert worker._release_for_retry(table, "job-a", "ask-a", "owner") is True
    assert table.request["ExpressionAttributeValues"][":stage"] == "RETRYING"


def test_processing_stage_update_is_guarded_by_current_live_lease():
    class Capture:
        name = "questions"

        def update_item(self, **kwargs):
            self.request = kwargs

    table = Capture()
    assert worker._set_processing_stage(table, "job-a", "ask-a", "owner", "WAITING_MODEL") is True
    assert table.request["ExpressionAttributeValues"][":stage"] == "WAITING_MODEL"
    assert "processing_owner = :owner" in table.request["ConditionExpression"]
    assert "processing_lease_until > :now" in table.request["ConditionExpression"]


def test_completion_transaction_condition_checks_parent_job_and_sets_completed_stage(monkeypatch):
    captured = {}

    class Client:
        def transact_write_items(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(worker, "dynamodb", type("Resource", (), {"meta": type("Meta", (), {"client": Client()})()})())
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    assert worker._complete_ask(
        Table(), "job-a", "ask-a", "user-a", "owner",
        {"answer": "safe", "source_pages": [1]}, False,
    ) is True
    condition, update = captured["TransactItems"]
    assert condition["ConditionCheck"]["TableName"] == "jobs"
    assert update["Update"]["ExpressionAttributeValues"][":stage"] == "COMPLETED"


def test_claim_sets_loading_context_stage_with_the_lease():
    class Capture:
        def update_item(self, **kwargs):
            self.request = kwargs
            return {"Attributes": {"status": "PROCESSING", "processing_stage": "LOADING_CONTEXT"}}

    table = Capture()
    claimed = worker._claim_ask(table, "job-a", "ask-a", "owner")
    assert claimed["processing_stage"] == "LOADING_CONTEXT"
    assert table.request["ExpressionAttributeValues"][":stage"] == "LOADING_CONTEXT"
    assert "#stage = :stage" in table.request["UpdateExpression"]


def test_lambda_handler_returns_partial_batch_failures_for_retryable_delivery(monkeypatch):
    captured = []
    monkeypatch.setattr(worker, "_process_message", lambda _record, context: captured.append(context) or False)
    event = {"Records": [record()]}
    context = object()
    assert worker.lambda_handler(event, context) == {"batchItemFailures": [{"itemIdentifier": "sqs-message-1"}]}
    assert captured == [context]


def test_effective_ollama_timeout_preserves_lambda_completion_margin(monkeypatch):
    monkeypatch.setenv("OLLAMA_ASK_READ_TIMEOUT", "780")

    class Context:
        def __init__(self, remaining_ms):
            self.remaining_ms = remaining_ms

        def get_remaining_time_in_millis(self):
            return self.remaining_ms

    assert worker._effective_ollama_read_timeout(Context(900_000)) == 780
    assert worker._effective_ollama_read_timeout(Context(600_000)) == 505
    assert worker._effective_ollama_read_timeout(Context(96_000)) == 1


def test_effective_ollama_timeout_skips_call_when_lambda_has_no_safe_margin(monkeypatch):
    monkeypatch.setenv("OLLAMA_ASK_READ_TIMEOUT", "780")

    class Context:
        def get_remaining_time_in_millis(self):
            return 95_000

    try:
        worker._effective_ollama_read_timeout(Context())
    except QuestionModelError as exc:
        assert exc.category == "insufficient_execution_time"
        assert exc.retryable is True
    else:
        raise AssertionError("Expected the worker to avoid starting a request too close to the Lambda timeout")


def test_effective_ollama_timeout_fails_closed_if_lambda_context_cannot_be_read(monkeypatch):
    monkeypatch.setenv("OLLAMA_ASK_READ_TIMEOUT", "780")

    class Context:
        def get_remaining_time_in_millis(self):
            raise RuntimeError("context unavailable")

    with pytest.raises(QuestionModelError) as failure:
        worker._effective_ollama_read_timeout(Context())
    assert failure.value.category == "execution_time_unavailable"
    assert failure.value.retryable is True


def test_malformed_non_object_sqs_body_is_acknowledged_without_worker_failure(monkeypatch):
    monkeypatch.setattr(worker, "_table", lambda *_args: (_ for _ in ()).throw(AssertionError("must not read table")))
    bad = record()
    bad["body"] = "[]"
    assert worker.lambda_handler({"Records": [bad]}, None) == {"batchItemFailures": []}
