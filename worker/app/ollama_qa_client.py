"""Ollama client for private, serialized document-question inference."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
import uuid

import requests

from app.qa_service import (
    ANSWER_SCHEMA,
    EVIDENCE_SCHEMA,
    InvalidAnswer,
    InvalidEvidence,
    QuestionContext,
    QuestionContextTooLarge,
    QuestionSection,
    build_evidence_messages,
    build_messages,
    estimate_upper_prompt_tokens,
    validate_answer,
    validate_evidence,
)

logger = logging.getLogger(__name__)


class QuestionModelError(RuntimeError):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
    ):
        self.category = category
        self.retryable = retryable
        self.public_message = message
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


def _contains_dns_error(exc: BaseException) -> bool:
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


def _read_timeout_for_call(configured: int, lambda_context) -> int:
    remaining = getattr(lambda_context, "get_remaining_time_in_millis", None)
    if not callable(remaining):
        return configured
    try:
        seconds = int(remaining()) // 1000
    except Exception as exc:
        raise QuestionModelError(
            "execution_time_unavailable",
            "No se pudo comprobar el tiempo restante de forma segura; se volverá a intentar.",
            retryable=True,
        ) from exc
    available = seconds - 90 - 5
    if available < 1:
        raise QuestionModelError(
            "insufficient_execution_time",
            "No quedó tiempo suficiente para iniciar la consulta con seguridad; se volverá a intentar.",
            retryable=True,
        )
    return min(configured, available)


def _response_json(response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise QuestionModelError(
            "invalid_response_envelope", "El servicio de IA devolvió una respuesta inválida", retryable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise QuestionModelError(
            "invalid_response_envelope", "El servicio de IA devolvió una respuesta inválida", retryable=True,
        )
    return payload


def _log_response_metadata(
    *, request_id: str, processing_stage: str, response, elapsed_ms: int,
    model: str, num_ctx: int, num_predict: int, envelope: dict,
) -> tuple[dict, str | None]:
    candidate = envelope.get("message")
    message = candidate if isinstance(candidate, dict) else {}
    content = message.get("content")
    thinking = message.get("thinking")
    content_length = len(content) if isinstance(content, str) else None
    thinking_length = len(thinking) if isinstance(thinking, str) else 0
    actual_model = envelope.get("model") if isinstance(envelope.get("model"), str) else None
    logger.info(
        "ASK_OLLAMA_RESPONSE_RECEIVED request_id=%s processing_stage=%s http_status=%s model=%s "
        "done=%s done_reason=%s prompt_eval_count=%s eval_count=%s prompt_eval_duration=%s "
        "eval_duration=%s total_duration=%s response_content_length=%s thinking_present=%s "
        "thinking_length=%s num_ctx=%d num_predict=%d",
        request_id, processing_stage, getattr(response, "status_code", None), actual_model or model,
        envelope.get("done"), envelope.get("done_reason"), envelope.get("prompt_eval_count"),
        envelope.get("eval_count"), envelope.get("prompt_eval_duration"), envelope.get("eval_duration"),
        envelope.get("total_duration"), content_length, isinstance(thinking, str) and bool(thinking),
        thinking_length, num_ctx, num_predict,
    )
    return message, actual_model


def _call_structured(
    messages: list[dict[str, str]],
    schema: dict,
    allowed_pages: tuple[int, ...],
    token: str,
    read_timeout: int,
    *,
    num_predict: int,
    lambda_context=None,
    request_id: str | None = None,
    processing_stage: str = "final_answer",
    require_citations: bool = False,
) -> tuple[dict, str | None]:
    base_url = os.environ["OLLAMA_BASE_URL"].rstrip("/")
    model = os.getenv("OLLAMA_ASK_MODEL", "qwen3-vl:8b-instruct")
    num_ctx = int(os.getenv("OLLAMA_ASK_NUM_CTX", "8192"))
    queue_wait = int(os.getenv("OLLAMA_ASK_QUEUE_WAIT_SECONDS", "120"))
    if num_ctx < 1 or num_predict < 1 or read_timeout < 1 or queue_wait < 1:
        raise ValueError("Ollama ask limits must be positive integers")

    request_id = request_id or str(uuid.uuid4())
    headers = {
        "X-Request-ID": request_id,
        "X-Coordinator-Queue-Wait-Seconds": str(queue_wait),
        "X-Coordinator-Request-Timeout-Seconds": str(read_timeout),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "messages": messages,
        "format": schema,
        "options": {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": 0},
    }
    started = time.monotonic()
    effective_timeout = _read_timeout_for_call(read_timeout, lambda_context)
    try:
        response = requests.post(
            f"{base_url}/api/chat",
            json=payload,
            headers=headers,
            timeout=(5, effective_timeout),
        )
    except requests.ConnectTimeout as exc:
        raise QuestionModelError("connect_timeout", "No se pudo conectar a tiempo con el servicio de IA", retryable=True) from exc
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
    if response.status_code == 503:
        payload_error = _response_json(response)
        error_code = payload_error.get("error")
        if error_code in {"coordinator_busy", "coordinator_unavailable", "coordinator_shutdown"}:
            try:
                retry_after = min(5400, max(1, int(response.headers.get("Retry-After", "900"))))
            except (AttributeError, TypeError, ValueError):
                retry_after = 900
            raise QuestionModelError(
                error_code,
                "La IA no está disponible para esta solicitud. Se volverá a intentar más tarde.",
                retryable=True,
                http_status=503,
                retry_after_seconds=retry_after,
            )
    if response.status_code >= 500 or response.status_code in {408, 429}:
        raise QuestionModelError(
            "http_error", "El servicio de IA no está disponible temporalmente",
            retryable=True, http_status=response.status_code,
        )
    if not response.ok:
        category = "authentication_error" if response.status_code in {401, 403} else "http_error"
        logger.warning(
            "ASK_OLLAMA_HTTP_ERROR request_id=%s processing_stage=%s http_status=%s model=%s elapsed_ms=%d error_category=%s",
            request_id, processing_stage, response.status_code, model, elapsed_ms, category,
        )
        raise QuestionModelError(category, "El servicio de IA rechazó la pregunta", http_status=response.status_code)

    envelope = _response_json(response)
    message, actual_model = _log_response_metadata(
        request_id=request_id, processing_stage=processing_stage, response=response,
        elapsed_ms=elapsed_ms, model=model, num_ctx=num_ctx, num_predict=num_predict, envelope=envelope,
    )
    if "error" in envelope:
        raise QuestionModelError("ollama_api_error", "El servicio de IA no pudo responder", retryable=True,
                                 http_status=response.status_code)
    if not message:
        raise QuestionModelError("invalid_response_envelope", "El servicio de IA devolvió una respuesta inválida", retryable=True)
    done_reason = envelope.get("done_reason")
    content = message.get("content")
    eval_count = envelope.get("eval_count")
    output_budget_reached = (
        isinstance(eval_count, int)
        and not isinstance(eval_count, bool)
        and eval_count >= num_predict
    )
    if done_reason == "length" and (not isinstance(content, str) or not content.strip()) and output_budget_reached:
        logger.warning(
            "ASK_OLLAMA_OUTPUT_LIMIT_REACHED request_id=%s processing_stage=%s model=%s num_predict=%d eval_count=%s",
            request_id, processing_stage, actual_model or model, num_predict, eval_count,
        )
        # The same empty response cannot recover from an identical exhausted
        # budget. Only this confirmed truncation is terminal; transport,
        # service, and other incomplete-response failures retain their retries.
        raise QuestionModelError(
            "output_budget_exhausted",
            "La IA agotó el límite de generación antes de completar una respuesta. Inténtalo nuevamente más tarde.",
            retryable=False,
            http_status=response.status_code,
        )
    if envelope.get("done") is False:
        raise QuestionModelError("incomplete_output", "La respuesta quedó incompleta; se volverá a intentar", retryable=True)
    if not isinstance(content, str) or not content.strip():
        category = "thinking_without_final_content" if isinstance(message.get("thinking"), str) and message["thinking"].strip() else "missing_content"
        logger.warning(
            "ASK_OLLAMA_RESPONSE_INVALID request_id=%s processing_stage=%s model=%s error_category=%s response_content_length=%s thinking_present=%s thinking_length=%s done_reason=%s eval_count=%s",
            request_id, processing_stage, actual_model or model, category,
            len(content) if isinstance(content, str) else None,
            isinstance(message.get("thinking"), str) and bool(message["thinking"]),
            len(message["thinking"]) if isinstance(message.get("thinking"), str) else 0,
            done_reason, envelope.get("eval_count"),
        )
        raise QuestionModelError(category, "El servicio de IA devolvió una respuesta vacía", retryable=True)

    if actual_model is not None and actual_model != model:
        raise QuestionModelError(
            "model_mismatch", "El servicio de IA respondió con una configuración inesperada.",
            retryable=False, http_status=response.status_code,
        )
    # Some Ollama model templates expose reasoning separately in
    # `message.thinking`; others include it in message.content. Never persist
    # or return reasoning as the final answer.
    reasoning_length = len(message.get("thinking")) if isinstance(message.get("thinking"), str) else 0
    if re.search(r"<think\b", content, flags=re.IGNORECASE):
        if not re.search(r"</think\s*>", content, flags=re.IGNORECASE):
            raise QuestionModelError(
                "incomplete_reasoning", "La respuesta de IA no incluyó una respuesta final completa", retryable=True,
            )
        reasoning_length += sum(
            len(match.group(0))
            for match in re.finditer(r"<think\b.*?</think\s*>", content, flags=re.IGNORECASE | re.DOTALL)
        )
    final_content = re.sub(r"<think\b.*?</think\s*>", "", content, flags=re.IGNORECASE | re.DOTALL)
    final_content = re.sub(r"^\s*</think\s*>\s*", "", final_content, flags=re.IGNORECASE)
    try:
        parsed = json.loads(final_content)
    except json.JSONDecodeError as exc:
        logger.warning(
            "ASK_OLLAMA_RESPONSE_JSON_INVALID request_id=%s processing_stage=%s schema_name=%s model=%s json_valid=false raw_length=%d done_reason=%s eval_count=%s error_category=invalid_json",
            request_id, processing_stage, "QuestionAnswer" if schema is ANSWER_SCHEMA else "QuestionEvidence",
            actual_model or model, len(final_content), done_reason, envelope.get("eval_count"),
        )
        raise QuestionModelError("invalid_json", "La respuesta de IA no tuvo un formato válido", retryable=True) from exc
    logger.info(
        "ASK_OLLAMA_RESPONSE_JSON_VALID request_id=%s processing_stage=%s schema_name=%s model=%s json_valid=true raw_length=%d",
        request_id, processing_stage, "QuestionAnswer" if schema is ANSWER_SCHEMA else "QuestionEvidence",
        actual_model or model, len(final_content),
    )
    thinking = message.get("thinking")
    try:
        validated = (
            validate_answer(parsed, allowed_pages, require_citations=require_citations)
            if schema is ANSWER_SCHEMA else parsed
        )
    except InvalidAnswer as exc:
        logger.warning(
            "ASK_OLLAMA_RESPONSE_SCHEMA_INVALID request_id=%s processing_stage=%s schema_name=%s model=%s error_category=invalid_answer",
            request_id, processing_stage, "QuestionAnswer" if schema is ANSWER_SCHEMA else "QuestionEvidence",
            actual_model or model,
        )
        raise QuestionModelError("invalid_answer", "La respuesta de IA no pudo validarse", retryable=True) from exc
    logger.info(
        "ASK_OLLAMA_RESPONSE_SCHEMA_VALID request_id=%s processing_stage=%s schema_name=%s model=%s",
        request_id, processing_stage, "QuestionAnswer" if schema is ANSWER_SCHEMA else "QuestionEvidence",
        actual_model or model,
    )
    return validated, actual_model


def _run_hierarchical(question: str, context: QuestionContext, token: str, read_timeout: int,
                      lambda_context=None, request_id: str | None = None) -> tuple[dict, str | None]:
    map_budget = min(300, int(os.getenv("OLLAMA_ASK_EVIDENCE_NUM_PREDICT", "300")))
    if map_budget < 1:
        raise ValueError("OLLAMA_ASK_EVIDENCE_NUM_PREDICT must be a positive integer")
    evidence: list[dict] = []
    confirmed_model: str | None = None
    for section_index, section in enumerate(context.sections, start=1):
        try:
            parsed, actual_model = _call_structured(
                build_evidence_messages(question, section), EVIDENCE_SCHEMA, section.page_numbers,
                token, read_timeout, num_predict=map_budget, lambda_context=lambda_context,
                request_id=f"{request_id}-evidence-{section_index}" if request_id else None,
                processing_stage="evidence_extraction",
            )
            confirmed_model = actual_model or confirmed_model
            items = validate_evidence(parsed, section)
        except InvalidEvidence as exc:
            raise QuestionModelError("invalid_evidence", "No se pudo verificar la evidencia de una sección", retryable=True) from exc
        evidence.extend(items)

    # Remove duplicate exact quotes and preserve the original page order.
    deduplicated: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for item in evidence:
        marker = (item["page"], " ".join(item["quote"].casefold().split()))
        if marker not in seen:
            seen.add(marker)
            deduplicated.append(item)
    evidence_context = QuestionContext(
        text=json.dumps(deduplicated, ensure_ascii=False, separators=(",", ":")),
        page_numbers=tuple(sorted({item["page"] for item in deduplicated})),
        context_limited=True,
        source_chunk_count=len(deduplicated),
        context_mode="HIERARCHICAL_FULL_TRANSCRIPT",
    )
    messages = build_messages(question, evidence_context)
    num_ctx = int(os.getenv("OLLAMA_ASK_NUM_CTX", "8192"))
    num_predict = int(os.getenv("OLLAMA_ASK_NUM_PREDICT", "2000"))
    if estimate_upper_prompt_tokens(messages, ANSWER_SCHEMA) + num_predict + 512 > num_ctx:
        raise QuestionContextTooLarge("La evidencia verificada excede el contexto para redactar una respuesta")
    answer, actual_model = _call_structured(
        messages, ANSWER_SCHEMA, evidence_context.page_numbers, token, read_timeout,
        num_predict=num_predict, lambda_context=lambda_context,
        request_id=f"{request_id}-answer" if request_id else None,
        processing_stage="final_synthesis",
        require_citations=True,
    )
    actual_model = actual_model or confirmed_model
    return {**answer, "model": actual_model, "context_mode": context.context_mode}, actual_model


def ask_ollama(
    question: str,
    context: QuestionContext,
    token: str,
    read_timeout_override: int | None = None,
    *,
    lambda_context=None,
    request_id: str | None = None,
) -> dict:
    model = os.getenv("OLLAMA_ASK_MODEL", "qwen3-vl:8b-instruct")
    num_ctx = int(os.getenv("OLLAMA_ASK_NUM_CTX", "8192"))
    num_predict = int(os.getenv("OLLAMA_ASK_NUM_PREDICT", "2000"))
    configured_timeout = read_timeout_override if read_timeout_override is not None else int(os.getenv("OLLAMA_ASK_READ_TIMEOUT", "780"))
    if num_ctx < 1 or num_predict < 1 or configured_timeout < 1:
        raise ValueError("Ollama ask limits must be positive integers")
    if context.context_mode == "HIERARCHICAL_FULL_TRANSCRIPT":
        answer, actual_model = _run_hierarchical(
            question, context, token, configured_timeout, lambda_context=lambda_context,
            request_id=request_id,
        )
        return answer

    messages = build_messages(question, context)
    if estimate_upper_prompt_tokens(messages, ANSWER_SCHEMA) + num_predict + 512 > num_ctx:
        raise QuestionContextTooLarge("La transcripción completa excede el contexto de inferencia configurado")
    answer, actual_model = _call_structured(
        messages, ANSWER_SCHEMA, context.page_numbers, token, configured_timeout,
        num_predict=num_predict, lambda_context=lambda_context, request_id=request_id,
        processing_stage="full_transcript_answer",
    )
    logger.info(
        "ASK_MODEL_RESPONSE_VALID model=%s source_pages=%d context_mode=%s",
        actual_model or "unreported", len(answer["source_pages"]), context.context_mode,
    )
    return {**answer, "model": actual_model, "context_mode": context.context_mode}
