"""Local API → SQS → question worker → API status integration using fakes only."""

import json
import time

from app import jobs, question_worker


class MemoryTable:
    def __init__(self, name, items=()):
        self.name = name
        self.items = {}
        for item in items:
            self.items[(item["job_id"], item.get("ask_id"))] = dict(item)

    def get_item(self, *, Key, **_kwargs):
        item = self.items.get((Key["job_id"], Key.get("ask_id")))
        return {"Item": dict(item)} if item else {}


class MemoryQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, **request):
        self.messages.append(request)


def api_event(method, resource, *, job_id, ask_id="", body=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": {"job_id": job_id, "ask_id": ask_id},
        "body": json.dumps(body or {}),
        "requestContext": {"authorizer": {"claims": {"sub": "user-123"}}},
    }


def test_question_completes_from_owned_persisted_result_and_is_visible_through_api(monkeypatch):
    job_id = "job-specific-document"
    job_table = MemoryTable("jobs", [{
        "job_id": job_id, "user_id": "user-123", "status": "COMPLETED",
        "result_key": f"results/{job_id}.json",
    }])
    question_table = MemoryTable("questions")
    queue = MemoryQueue()
    captured = {"result_keys": [], "contexts": []}

    monkeypatch.setattr(jobs, "_table", lambda: job_table)
    monkeypatch.setattr(jobs, "_question_table", lambda: question_table)
    monkeypatch.setattr(jobs, "sqs", queue)
    monkeypatch.setenv("QUESTION_QUEUE_URL", "questions-url")
    monkeypatch.setattr(jobs, "MAX_USER_QUESTIONS_PER_DAY", 50)

    def create_transaction(checked_job_id, user_id, ask, rate_key):
        assert checked_job_id == job_id
        assert user_id == "user-123"
        assert job_table.items[(job_id, None)]["status"] == "COMPLETED"
        question_table.items[(job_id, ask["ask_id"])] = dict(ask)
        rate_key_id = (rate_key["job_id"], rate_key["ask_id"])
        question_table.items[rate_key_id] = {**rate_key, "question_count": 1}

    monkeypatch.setattr(jobs, "_transact_create_ask", create_transaction)
    monkeypatch.setattr(jobs, "_question_quota_count", lambda _key: 1)
    created = jobs.lambda_handler(api_event(
        "POST", "/jobs/{job_id}/ask", job_id=job_id,
        body={"question": "¿Qué establece el documento?"},
    ), None)
    created_body = json.loads(created["body"])
    assert created["statusCode"] == 202
    ask_id = created_body["ask_id"]
    message_body = json.loads(queue.messages[0]["MessageBody"])
    assert message_body == {"job_id": job_id, "ask_id": ask_id, "user_id": "user-123"}
    assert "question" not in message_body

    monkeypatch.setattr(question_worker, "_table", lambda name: {
        "QUESTION_TABLE": question_table,
        "JOBS_TABLE": job_table,
    }[name])
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    monkeypatch.setenv("RESULTS_BUCKET", "private-results")
    monkeypatch.setenv("OLLAMA_ASK_CONTEXT_MAX_CHARS", "16000")
    monkeypatch.setattr(question_worker, "_token", lambda: "")

    def claim(_table, claimed_job_id, claimed_ask_id, owner):
        assert claimed_job_id == job_id
        item = question_table.items[(claimed_job_id, claimed_ask_id)]
        assert item["status"] == "PENDING"
        item.update(status="PROCESSING", processing_owner=owner, processing_attempt=1,
                    processing_lease_until=int(time.time()) + 960)
        return dict(item)

    monkeypatch.setattr(question_worker, "_claim_ask", claim)

    def load_result(bucket, key):
        assert bucket == "private-results"
        captured["result_keys"].append(key)
        return {
            "pages": 2,
            "page_extractions": [
                {"page": 1, "transcription": "Portada", "analysis_notes": "", "visual_elements": {}},
                {"page": 2, "transcription": "Obligación: entregar el informe mensual.", "analysis_notes": "", "visual_elements": {}},
            ],
        }

    monkeypatch.setattr(question_worker, "_load_result", load_result)
    monkeypatch.setattr(question_worker, "ask_ollama", lambda _question, context, _token, _timeout: (
        captured["contexts"].append(context.text) or {"answer": "Entregar el informe mensual.", "source_pages": [2]}
    ))

    def complete(_table, completed_job_id, completed_ask_id, user_id, owner, answer, context_limited):
        assert (completed_job_id, completed_ask_id, user_id) == (job_id, ask_id, "user-123")
        assert job_table.items[(job_id, None)]["status"] == "COMPLETED"
        item = question_table.items[(job_id, ask_id)]
        assert item["status"] == "PROCESSING" and item["processing_owner"] == owner
        item.update(status="COMPLETED", answer=answer["answer"], source_pages=answer["source_pages"],
                    context_limited=context_limited)
        item.pop("processing_owner")
        item.pop("processing_lease_until")
        return True

    monkeypatch.setattr(question_worker, "_complete_ask", complete)
    event = {
        "Records": [{
            "messageId": "queue-message", "receiptHandle": "receipt",
            "attributes": {"ApproximateReceiveCount": "1"},
            "body": queue.messages[0]["MessageBody"],
        }],
    }
    assert question_worker.lambda_handler(event, None) == {"batchItemFailures": []}
    assert captured["result_keys"] == [f"results/{job_id}.json"]
    assert "Obligación: entregar el informe mensual." in captured["contexts"][0]

    status = jobs.lambda_handler(api_event(
        "GET", "/jobs/{job_id}/asks/{ask_id}", job_id=job_id, ask_id=ask_id,
    ), None)
    status_body = json.loads(status["body"])
    assert status["statusCode"] == 200
    assert status_body["job_id"] == job_id
    assert status_body["ask_id"] == ask_id
    assert status_body["status"] == "COMPLETED"
    assert status_body["answer"] == "Entregar el informe mensual."
    assert status_body["source_pages"] == [2]
