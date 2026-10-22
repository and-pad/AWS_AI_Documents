import json
import logging
import socket

import pytest
import requests

from app import ollama_qa_client
from app.qa_service import QuestionContext
from app.ollama_qa_client import QuestionModelError, ask_ollama


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400

    def json(self):
        return self.payload


def context():
    return QuestionContext('[{"page":2,"kind":"transcripción","text":"Nombre: Ana"}]', (2,), False, 1)


def test_question_call_uses_structured_output_and_separate_token_budget(monkeypatch):
    calls = []
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test:11443")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3-vl:8b-instruct")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
    monkeypatch.setenv("OLLAMA_ASK_NUM_PREDICT", "1000")
    monkeypatch.setenv("OLLAMA_ASK_READ_TIMEOUT", "780")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or Response({
            "message": {"content": json.dumps({"answer": "Ana", "source_pages": [2]})},
            "done": True,
            "done_reason": "stop",
        })
    ))

    answer = ask_ollama("¿Quién aparece?", context(), "private-token")

    assert answer == {"answer": "Ana", "source_pages": [2]}
    request = calls[0][1]
    assert request["timeout"] == (5, 780)
    assert request["headers"]["Authorization"] == "Bearer private-token"
    assert request["json"]["stream"] is False
    assert request["json"]["options"] == {"num_ctx": 16384, "num_predict": 1000, "temperature": 0}
    assert request["json"]["format"]["additionalProperties"] is False


@pytest.mark.parametrize("exception, category", [
    (requests.ReadTimeout("timeout"), "read_timeout"),
    (requests.ConnectionError("down"), "connection_error"),
    (requests.exceptions.SSLError("certificate invalid"), "tls_error"),
    (requests.ConnectionError("dns", socket.gaierror(-2, "private-host")), "dns_error"),
])
def test_transport_errors_are_classified_without_logging_private_content(monkeypatch, caplog, exception, category):
    token, question = "secret-token-value", "pregunta privada"
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_args, **_kwargs: (_ for _ in ()).throw(exception))
    with caplog.at_level(logging.INFO), pytest.raises(QuestionModelError) as error:
        ask_ollama(question, context(), token)
    assert error.value.category == category
    assert token not in caplog.text
    assert question not in caplog.text
    assert token not in str(error.value)


@pytest.mark.parametrize("status", [500, 502, 504])
def test_http_gateway_failures_preserve_status_and_are_retryable(monkeypatch, status):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({}, status=status))
    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("private question", context(), "secret")
    assert failure.value.category == "http_error"
    assert failure.value.http_status == status
    assert failure.value.retryable is True


def test_ollama_api_error_envelope_is_classified_without_logging_its_payload(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({"error": "private backend detail"}))

    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("private question", context(), "secret-token")

    assert failure.value.category == "ollama_api_error"
    assert failure.value.retryable is True
    assert "private backend detail" not in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_bad_bearer_is_permanent_without_echoing_token(monkeypatch, caplog, status):
    secret = "do-not-log-token"
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({}, status=status))
    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("private question", context(), secret)
    assert failure.value.category == "authentication_error"
    assert failure.value.http_status == status
    assert failure.value.retryable is False
    assert secret not in caplog.text
    assert secret not in str(failure.value)


def test_json_and_truncation_errors_do_not_return_unvalidated_answers(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "message": {"content": '{"answer":'}, "done": True, "done_reason": "stop",
    }))
    with pytest.raises(QuestionModelError) as invalid_json:
        ask_ollama("pregunta", context(), "")
    assert invalid_json.value.category == "invalid_json"
    assert invalid_json.value.retryable is True

    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "message": {"content": '{"answer":"Ana","source_pages":[2]}'}, "done": False, "done_reason": "length",
    }))
    with pytest.raises(QuestionModelError) as incomplete:
        ask_ollama("pregunta", context(), "")
    assert incomplete.value.category == "incomplete_output"
    assert incomplete.value.retryable is True


def test_http_server_error_can_be_retried_but_invalid_page_reference_cannot(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({}, status=503))
    with pytest.raises(QuestionModelError) as server_error:
        ask_ollama("pregunta", context(), "")
    assert server_error.value.retryable is True

    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "message": {"content": '{"answer":"Otra página","source_pages":[3]}'}, "done": True,
    }))
    with pytest.raises(QuestionModelError) as invalid_page:
        ask_ollama("pregunta", context(), "")
    assert invalid_page.value.category == "invalid_answer"
    assert invalid_page.value.retryable is True

