"""Small Ollama client for the asynchronous document-question Lambda."""

from __future__ import annotations

import json
import logging
import os
import socket
import time

import requests

from app.qa_service import ANSWER_SCHEMA, InvalidAnswer, QuestionContext, build_messages, validate_answer

logger = logging.getLogger(__name__)


class QuestionModelError(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool = False, http_status: int | None = None):
        self.category = category
        self.retryable = retryable
        self.public_message = message
        self.http_status = http_status
        super().__init__(message)


def _contains_dns_error(exc: BaseException) -> bool:
    """Classify nested requests/urllib3 DNS causes without logging their text."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return True
        for nested in (current.__cause__, current.__context__, *current.args):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def ask_ollama(question: str, context: QuestionContext, token: str, read_timeout_override: int | None = None) -> dict:
    base_url = os.environ["OLLAMA_BASE_URL"].rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen3-vl:8b-instruct")
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
    num_predict = int(os.getenv("OLLAMA_ASK_NUM_PREDICT", "1000"))
    read_timeout = read_timeout_override if read_timeout_override is not None else int(os.getenv("OLLAMA_ASK_READ_TIMEOUT", "780"))
    if num_ctx < 1 or num_predict < 1 or read_timeout < 1:
        raise ValueError("Ollama ask limits must be positive integers")

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request_payload = {
        "model": model,
        "stream": False,
        "messages": build_messages(question, context),
        "format": ANSWER_SCHEMA,
        "options": {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": 0},
    }
    started = time.monotonic()
    try:
        response = requests.post(
            f"{base_url}/api/chat",
            json=request_payload,
            headers=headers,
            timeout=(5, read_timeout),
        )
    except requests.ReadTimeout as exc:
        raise QuestionModelError("read_timeout", "La respuesta de IA tardó demasiado", retryable=True) from exc
    except requests.exceptions.SSLError as exc:
        raise QuestionModelError("tls_error", "No se pudo validar la conexión segura con el servicio de IA") from exc
    except requests.ConnectionError as exc:
        if _contains_dns_error(exc):
            raise QuestionModelError("dns_error", "No se pudo resolver el servicio de IA", retryable=True) from exc
        raise QuestionModelError("connection_error", "No se pudo conectar con el servicio de IA", retryable=True) from exc
    except requests.RequestException as exc:
        raise QuestionModelError("connection_error", "Falló la solicitud al servicio de IA", retryable=True) from exc

    elapsed_ms = round((time.monotonic() - started) * 1000)
    if response.status_code >= 500 or response.status_code in {408, 429}:
        raise QuestionModelError(
            "http_error", "El servicio de IA no está disponible temporalmente",
            retryable=True, http_status=response.status_code,
        )
    if not response.ok:
        category = "authentication_error" if response.status_code in {401, 403} else "http_error"
        raise QuestionModelError(category, "El servicio de IA rechazó la pregunta", http_status=response.status_code)
    logger.info("ASK_OLLAMA_HTTP_RESPONSE http_status=%d elapsed_ms=%d", response.status_code, elapsed_ms)
    try:
        envelope = response.json()
    except ValueError as exc:
        raise QuestionModelError("invalid_response_envelope", "El servicio de IA devolvió una respuesta inválida", retryable=True) from exc
    if not isinstance(envelope, dict):
        raise QuestionModelError("invalid_response_envelope", "El servicio de IA devolvió una respuesta inválida", retryable=True)
    if "error" in envelope:
        raise QuestionModelError("ollama_api_error", "El servicio de IA no pudo responder", retryable=True,
                                 http_status=response.status_code)
    if not isinstance(envelope.get("message"), dict):
        raise QuestionModelError("invalid_response_envelope", "El servicio de IA devolvió una respuesta inválida", retryable=True)
    message = envelope["message"]
    raw_content = message.get("content")
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise QuestionModelError("missing_content", "El servicio de IA devolvió una respuesta vacía", retryable=True)
    done_reason = envelope.get("done_reason")
    if envelope.get("done") is False or done_reason == "length":
        raise QuestionModelError("incomplete_output", "La respuesta quedó incompleta; se volverá a intentar", retryable=True)
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise QuestionModelError("invalid_json", "La respuesta de IA no tuvo un formato válido", retryable=True) from exc
    try:
        answer = validate_answer(parsed, context.page_numbers)
    except InvalidAnswer as exc:
        raise QuestionModelError("invalid_answer", "La respuesta de IA no pudo validarse", retryable=True) from exc

    logger.info(
        "Document question model response valid model=%s source_pages=%d context_limited=%s",
        model,
        len(answer["source_pages"]),
        context.context_limited,
    )
    return answer
