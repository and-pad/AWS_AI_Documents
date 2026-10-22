import json
import logging
import re
import time
import hashlib
import uuid
from collections.abc import Callable

import pymupdf as fitz
from pydantic import ValidationError
from botocore.exceptions import ClientError

from app.ollama_client import OllamaClient, OllamaError
from app.schemas import (
    DocumentAnalysis,
    DocumentAnalysisGeneration,
    PageExtraction,
    RequestedOutput,
    Transcription,
)
from app.settings import get_settings
from app.diagnostics import current_diagnostic_context, diagnostic_context, log_event, log_sensitive_model_output

logger = logging.getLogger(__name__)

PAGE_EXTRACTION_SCHEMA_VERSION = "page-extraction-v1"

PAGE_PROMPT = """Analiza únicamente la imagen adjunta de esta página de un documento en español.
No completes texto ausente ni uses el contexto previo como texto visible de esta página.
Extrae la transcripción de forma literal, preservando encabezados, párrafos, nombres, fechas, cantidades,
folios y errores visibles. No resumas ni corrijas la transcripción. Si un fragmento no puede leerse con
suficiente certeza escribe [ILEGIBLE], sin adivinar.
En analysis_notes anota solo los hechos relevantes de esta página para la síntesis del documento.
Describe sellos, firmas, rúbricas, logotipos relevantes, anotaciones manuscritas y otras marcas visuales.
No afirmes autenticidad ni identifiques personas por la forma de una firma. Usa arrays vacíos si no hay
elementos de una categoría. Las ubicaciones son aproximadas y deben mencionar esta página.
El context_summary debe ser breve y útil para interpretar páginas posteriores.
Sigue exactamente el JSON Schema indicado. Devuelve solo JSON válido, sin Markdown ni texto adicional."""

FINAL_PROMPT = """Consolida el documento completo usando únicamente las extracciones por página que se proporcionan.
Devuelve un análisis conciso, profesional y en español, sin repetir la transcripción.
analysis.document_type identifica el tipo aparente. analysis.summary explica qué documento parece ser, de qué
trata, participantes claramente identificables, fechas, folios, cantidades y propósito aparente.
analysis.key_points contiene solo datos relevantes y concisos, no párrafos completos.
La transcripción literal ya fue extraída por página y será ensamblada por el backend; no intentes reproducirla.
Conserva sellos, firmas y otras marcas visuales observadas. No afirmes autenticidad ni identifiques personas
solo por la forma de una firma.
requested_output contiene únicamente response. El backend agrega user_request con el texto original recibido.
Si no hay petición adicional, usa response null. Si existe una petición, responde basándote solo en el documento;
si se pide redactar, redacta una propuesta sustentada en el contenido.
Sigue exactamente el JSON Schema indicado. Devuelve solo JSON válido, sin Markdown, comentarios ni texto externo."""


MAX_STRUCTURED_OUTPUT_ATTEMPTS = 2
_SAFE_PATH_FIELDS = {
    "analysis", "document_type", "summary", "key_points", "transcription", "text",
    "visual_elements", "stamps", "description", "visible_text", "location",
    "signatures", "associated_name", "other_marks", "type", "analysis_notes",
    "context_summary", "requested_output", "response", "user_request",
}
_PYDANTIC_MESSAGES = {
    "missing": "Required field is missing",
    "extra_forbidden": "Additional field is not allowed",
    "string_type": "Input should be a valid string",
    "list_type": "Input should be a valid list",
    "dict_type": "Input should be a valid object",
    "model_type": "Input should be a valid object",
    "none_required": "Input should be null",
    "string_unicode": "Input should be valid text",
    "int_type": "Input should be a valid integer",
    "bool_type": "Input should be a valid boolean",
}
_JSON_PARSER_MESSAGES = {
    "Expecting value": "Expecting a JSON value",
    "Expecting property name enclosed in double quotes": "Expecting a quoted JSON property name",
    "Expecting ':' delimiter": "Expecting a property separator",
    "Expecting ',' delimiter": "Expecting a comma separator",
    "Unterminated string starting at": "Unterminated JSON string",
    "Extra data": "Unexpected data after JSON value",
}


class StructuredOutputError(OllamaError):
    """A safe, classified error for invalid model JSON or schema output."""

    def __init__(self, kind: str, schema_name: str, *, position: int | None = None,
                 parser_message: str | None = None, errors: list[dict] | None = None):
        self.kind = kind
        self.schema_name = schema_name
        self.position = position
        self.parser_message = parser_message
        self.errors = errors or []
        super().__init__(f"Salida estructurada inválida ({kind}, schema={schema_name})")


def _safe_path(location: tuple | list) -> str:
    parts = []
    for part in location:
        if isinstance(part, int):
            parts.append(str(part))
        elif isinstance(part, str) and part in _SAFE_PATH_FIELDS:
            parts.append(part)
        else:
            # Unknown keys can themselves contain document text; never log them.
            parts.append("<extra>")
    return ".".join(parts) or "<root>"


def _safe_validation_errors(exc: ValidationError) -> list[dict]:
    safe_errors = []
    for error in exc.errors():
        error_type = error.get("type", "unknown")
        if not isinstance(error_type, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", error_type):
            error_type = "unknown"
        safe_errors.append({
            "path": _safe_path(error.get("loc", ())),
            "type": error_type,
            "message": _PYDANTIC_MESSAGES.get(error_type, "Validation failed"),
        })
    return safe_errors


def _validate_model_output(raw: str, schema: type):
    schema_name = schema.__name__
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        failure = StructuredOutputError(
            "invalid_json",
            schema_name,
            position=exc.pos,
            parser_message=_JSON_PARSER_MESSAGES.get(exc.msg, "Invalid JSON syntax"),
        )
    except (TypeError, ValueError):
        failure = StructuredOutputError("invalid_json", schema_name, parser_message="Invalid JSON syntax")
    else:
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            failure = StructuredOutputError(
                "schema_invalid",
                schema_name,
                errors=_safe_validation_errors(exc),
            )
        except (TypeError, ValueError):
            failure = StructuredOutputError("schema_invalid", schema_name)

    # Raise outside the except blocks so raw parser/Pydantic exceptions (which can
    # retain model output and document values) are not attached as context.
    raise failure from None


def _model_output_metadata(raw: str) -> dict:
    metadata = getattr(raw, "metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def _original_model_output(raw: str) -> str:
    original = getattr(raw, "raw_content", raw)
    return original if isinstance(original, str) else str(raw)


def _generation_termination(metadata: dict, num_predict: int) -> tuple[str, str | None]:
    """Classify documented Ollama completion signals without guessing from proximity.

    `done_reason=stop` is affirmative normal termination, even when the output
    count is close to its budget. `length` means Ollama stopped for a length
    limit. Reaching the exact configured budget without an affirmative stop is
    treated as possible truncation; a merely nearby count is not.
    """
    done = metadata.get("done")
    reason = metadata.get("done_reason")
    reason = reason.strip().lower() if isinstance(reason, str) else None
    count = metadata.get("eval_count")
    count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None

    if reason == "length":
        return "limit_reached", "output_limit_reached"
    if done is False:
        return "confirmed_incomplete", "output_incomplete"
    if reason == "stop":
        return "normal", None
    if count is not None and count >= num_predict:
        return "possible_limit_reached", "possible_output_limit"
    if done is True:
        return "completed_reason_unknown", None
    return "metadata_unavailable", None


def _log_structured_output_error(
    error: StructuredOutputError,
    attempt: int,
    raw: str,
    *,
    elapsed_ms: int,
) -> None:
    original = _original_model_output(raw)
    metadata = _model_output_metadata(raw)
    category = "invalid_json" if error.kind == "invalid_json" else "schema_invalid"
    validation = {
        **current_diagnostic_context(),
        "schema_name": error.schema_name,
        "attempt": attempt,
        "validation_error_category": category,
        "error_type": "JSONDecodeError" if error.kind == "invalid_json" else "ValidationError",
        "raw_length": len(original),
        "response_content_length": len(original),
        "response_content_sha256": hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest(),
        "done_reason": metadata.get("done_reason"),
        "done": metadata.get("done"),
        "eval_count": metadata.get("eval_count"),
        "prompt_eval_count": metadata.get("prompt_eval_count"),
        "prompt_eval_duration": metadata.get("prompt_eval_duration"),
        "eval_duration": metadata.get("eval_duration"),
        "total_duration": metadata.get("total_duration"),
        "load_duration": metadata.get("load_duration"),
        "thinking_length": metadata.get("thinking_chars"),
        "elapsed_ms": elapsed_ms,
        "json_valid": error.kind != "invalid_json",
        "schema_valid": False,
        "error_count": len(error.errors) if error.errors else 1,
        "parser_position": error.position,
        "parser_message": error.parser_message,
        "field_path": ",".join(item["path"] for item in error.errors[:10]) if error.errors else None,
    }
    if error.kind == "invalid_json":
        log_event(
            logger,
            "OLLAMA_RESPONSE_INVALID_JSON",
            level=logging.WARNING,
            schema_name=error.schema_name,
            attempt=attempt,
            validation_error_category=category,
            error_type="JSONDecodeError",
            parser_position=error.position,
            parser_message=error.parser_message,
            error_count=1,
            raw_length=len(original),
            response_content_length=len(original),
            response_content_sha256=validation["response_content_sha256"],
            done_reason=metadata.get("done_reason"),
            elapsed_ms=elapsed_ms,
            json_valid=False,
            schema_valid=False,
        )
    else:
        log_event(
            logger,
            "OLLAMA_RESPONSE_SCHEMA_INVALID",
            level=logging.WARNING,
            schema_name=error.schema_name,
            attempt=attempt,
            validation_error_category=category,
            error_type="ValidationError",
            error_count=len(error.errors),
            raw_length=len(original),
            response_content_length=len(original),
            response_content_sha256=validation["response_content_sha256"],
            done_reason=metadata.get("done_reason"),
            elapsed_ms=elapsed_ms,
            json_valid=True,
            schema_valid=False,
        )
    for item in error.errors:
        log_event(
            logger,
            "OLLAMA_RESPONSE_SCHEMA_FIELD_INVALID",
            level=logging.WARNING,
            schema_name=error.schema_name,
            attempt=attempt,
            validation_error_category=category,
            error_count=len(error.errors),
            field_path=item["path"],
            error_type=item["type"],
        )
    # This event contains PII-capable model output and is emitted only by an
    # explicit DEBUG_MODEL_OUTPUT=true. The task log group has finite retention.
    log_sensitive_model_output(logger, response_content=original, validation=validation)


def _correction_message(error: StructuredOutputError) -> str:
    details = ""
    if error.errors:
        safe_paths = ", ".join(f"{item['path']} ({item['type']})" for item in error.errors)
        details = f"\nRutas que deben revisarse: {safe_paths}."
    return (
        "La respuesta anterior no fue JSON válido o no cumplió el JSON Schema. "
        "Genera de nuevo la respuesta completa siguiendo exactamente el mismo schema. "
        "No agregues propiedades. Incluye todos los campos requeridos; usa null para datos "
        "opcionales ausentes y arrays vacíos cuando no haya elementos. No conviertas tipos, "
        "no inventes datos y no incluyas Markdown ni texto externo."
        f"{details}"
    )


def _generate_validated(
    client: OllamaClient,
    messages: list[dict],
    schema: type,
    *,
    image: bytes | None = None,
    page: int | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
    context_summary_length: int | None = None,
):
    json_schema = schema.model_json_schema()
    settings = get_settings()
    request_stage = "page_extraction" if schema is PageExtraction else "final_synthesis"
    num_predict = (
        settings["ollama_page_num_predict"]
        if request_stage == "page_extraction"
        else settings["ollama_synthesis_num_predict"]
    )
    model = getattr(client, "model", settings["ollama_model"])
    timeout = getattr(client, "timeout", (None, settings["ollama_read_timeout"]))
    timeout_seconds = timeout[1] if isinstance(timeout, (tuple, list)) and len(timeout) > 1 else settings["ollama_read_timeout"]
    connect_timeout_seconds = timeout[0] if isinstance(timeout, (tuple, list)) and timeout else None
    last_request_id = None
    for attempt in range(1, MAX_STRUCTURED_OUTPUT_ATTEMPTS + 1):
        request_messages = [dict(message) for message in messages]
        call_options = {"options": {
            "num_ctx": settings["ollama_num_ctx"],
            "num_predict": num_predict,
        }}
        if attempt > 1:
            # Keep the image and the full extraction instructions in the same
            # user message on retry. Never include the rejected model content.
            request_messages[-1]["content"] += "\n\n" + _correction_message(last_error)
            # Keep normal sampling: temperature=0 reached a 1200-second read
            # timeout in the controlled local qwen3-vl:8b reproduction.
        started = time.monotonic()
        request_id = str(uuid.uuid4())
        last_request_id = request_id
        prompt_length = sum(len(message.get("content", "")) for message in request_messages)
        request_context = {
            "processing_stage": request_stage,
            "page_number": page,
            "schema_name": schema.__name__,
            "ollama_request_id": request_id,
            "attempt": attempt,
            "model": model,
        }
        with diagnostic_context(**request_context):
            log_event(
                logger,
                "OLLAMA_REQUEST_START",
                processing_stage=request_stage,
                page_number=page,
                schema_name=schema.__name__,
                ollama_request_id=request_id,
                attempt=attempt,
                model=model,
                message_count=len(request_messages),
                image_count=int(image is not None),
                image_sha256=hashlib.sha256(image).hexdigest() if image is not None else None,
                image_bytes=len(image) if image is not None else None,
                image_width=image_width,
                image_height=image_height,
                image_format="PNG" if image is not None else None,
                prompt_length=prompt_length,
                context_summary_length=context_summary_length,
                num_ctx=call_options["options"].get("num_ctx"),
                num_predict=num_predict,
                read_timeout=timeout_seconds,
                connect_timeout_seconds=connect_timeout_seconds,
                timeout_seconds=timeout_seconds,
            )
            try:
                raw = client.chat(
                    request_messages,
                    image=image,
                    format=json_schema,
                    **call_options,
                )
            except OllamaError as exc:
                log_event(
                    logger,
                    "OLLAMA_REQUEST_FAILED",
                    level=logging.ERROR,
                    processing_stage=request_stage,
                    page_number=page,
                    schema_name=schema.__name__,
                    ollama_request_id=request_id,
                    attempt=attempt,
                    model=model,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    request_elapsed_ms=round((time.monotonic() - started) * 1000),
                    timeout_seconds=timeout_seconds,
                    connect_timeout_seconds=connect_timeout_seconds,
                    error_category=exc.category,
                    error_type=type(exc).__name__,
                    http_status=exc.status_code,
                )
                raise
            elapsed_ms = round((time.monotonic() - started) * 1000)
            metadata = _model_output_metadata(raw)
            original = _original_model_output(raw)
            truncation_status, termination_error = _generation_termination(metadata, num_predict)
            log_event(
                logger,
                "OLLAMA_REQUEST_COMPLETED",
                processing_stage=request_stage,
                page_number=page,
                schema_name=schema.__name__,
                ollama_request_id=request_id,
                attempt=attempt,
                model=model,
                elapsed_ms=elapsed_ms,
                request_elapsed_ms=elapsed_ms,
                response_content_length=len(original),
                response_content_sha256=hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest(),
                http_status=metadata.get("http_status"),
                content_type=metadata.get("content_type"),
                done=metadata.get("done"),
                done_reason=metadata.get("done_reason"),
                prompt_eval_count=metadata.get("prompt_eval_count"),
                eval_count=metadata.get("eval_count"),
                prompt_eval_duration=metadata.get("prompt_eval_duration"),
                eval_duration=metadata.get("eval_duration"),
                total_duration=metadata.get("total_duration"),
                load_duration=metadata.get("load_duration"),
                thinking_length=metadata.get("thinking_chars"),
                num_ctx=call_options["options"]["num_ctx"],
                num_predict=num_predict,
                read_timeout=timeout_seconds,
                truncation_status=truncation_status,
                json_valid=None,
                schema_valid=None,
            )
            if termination_error:
                event = (
                    "OLLAMA_OUTPUT_LIMIT_REACHED"
                    if termination_error == "output_limit_reached"
                    else "OLLAMA_OUTPUT_INCOMPLETE"
                )
                log_event(
                    logger,
                    event,
                    level=logging.ERROR,
                    processing_stage=request_stage,
                    page_number=page,
                    schema_name=schema.__name__,
                    ollama_request_id=request_id,
                    attempt=attempt,
                    model=model,
                    num_ctx=call_options["options"]["num_ctx"],
                    num_predict=num_predict,
                    read_timeout=timeout_seconds,
                    done=metadata.get("done"),
                    done_reason=metadata.get("done_reason"),
                    eval_count=metadata.get("eval_count"),
                    prompt_eval_count=metadata.get("prompt_eval_count"),
                    prompt_eval_duration=metadata.get("prompt_eval_duration"),
                    total_duration=metadata.get("total_duration"),
                    eval_duration=metadata.get("eval_duration"),
                    elapsed_ms=elapsed_ms,
                    http_status=metadata.get("http_status"),
                    response_content_length=len(original),
                    truncation_status=truncation_status,
                    error_category=termination_error,
                )
                # A known exhausted generation must not be repeated immediately
                # with the same budget. Let the existing SQS retry/DLQ policy
                # handle the controlled retry without accepting this content.
                raise OllamaError(
                    "Ollama alcanzó el presupuesto de generación o devolvió una salida incompleta",
                    category=termination_error,
                    page_number=page,
                    request_id=request_id,
                    processing_stage=request_stage,
                ) from None
            log_event(
                logger,
                "OLLAMA_RESPONSE_VALIDATION_START",
                processing_stage=request_stage,
                page_number=page,
                schema_name=schema.__name__,
                ollama_request_id=request_id,
                attempt=attempt,
                raw_length=len(original),
                done_reason=metadata.get("done_reason"),
                truncation_status=truncation_status,
            )
            try:
                validated = _validate_model_output(raw, schema)
            except StructuredOutputError as error:
                _log_structured_output_error(error, attempt, raw, elapsed_ms=elapsed_ms)
                if attempt == MAX_STRUCTURED_OUTPUT_ATTEMPTS:
                    final_error_kind = error.kind
                    break
                last_error = error
                log_event(
                    logger,
                    "PAGE_RETRY_START" if request_stage == "page_extraction" else "SYNTHESIS_RETRY_START",
                    level=logging.WARNING,
                    processing_stage=request_stage,
                    page_number=page,
                    schema_name=schema.__name__,
                    ollama_request_id=request_id,
                    attempt=attempt + 1,
                    retry_attempt=attempt + 1,
                    validation_error_category=error.kind,
                    error_count=len(error.errors) if error.errors else 1,
                )
                continue
            log_event(
                logger,
                "OLLAMA_RESPONSE_VALID",
                processing_stage=request_stage,
                page_number=page,
                schema_name=schema.__name__,
                ollama_request_id=request_id,
                attempt=attempt,
                raw_length=len(original),
                response_content_length=len(original),
                response_content_sha256=hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest(),
                done_reason=metadata.get("done_reason"),
                truncation_status=truncation_status,
                num_ctx=call_options["options"]["num_ctx"],
                num_predict=num_predict,
                elapsed_ms=elapsed_ms,
                json_valid=True,
                schema_valid=True,
            )
            return validated

    raise OllamaError(
        f"Ollama devolvió salida {final_error_kind} inválida para {schema.__name__} "
        f"después de {MAX_STRUCTURED_OUTPUT_ATTEMPTS} intentos",
        category=final_error_kind,
        page_number=page,
        request_id=last_request_id,
        processing_stage=request_stage,
    ) from None


def _bound_context(value: str, max_chars: int) -> str:
    value = value.strip()
    if len(value) <= max_chars:
        return value
    if max_chars < 32:
        return value[:max_chars]
    marker = "\n… contexto intermedio omitido …\n"
    half = (max_chars - len(marker)) // 2
    return value[:half] + marker + value[-half:]


def _checkpoint_fingerprint(settings: dict) -> str:
    """Fingerprint only configuration that can change page extraction output."""
    from app.schemas import PageExtraction

    identity = {
        "schema_version": PAGE_EXTRACTION_SCHEMA_VERSION,
        "model": settings["ollama_model"],
        "render_dpi": settings["render_dpi"],
        "context_max_chars": settings["context_max_chars"],
        "num_ctx": settings["ollama_num_ctx"],
        "page_num_predict": settings["ollama_page_num_predict"],
        "page_prompt": PAGE_PROMPT,
        "schema": PageExtraction.model_json_schema(),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def result_pipeline_fingerprint(user_prompt: str | None, settings: dict | None = None) -> str:
    """Identify the extraction and synthesis contracts used for one final result."""
    from app.schemas import DocumentAnalysisGeneration

    settings = settings or get_settings()
    prompt_hash = hashlib.sha256((user_prompt or "").encode("utf-8")).hexdigest()
    identity = {
        "page_pipeline": _checkpoint_fingerprint(settings),
        "final_prompt": FINAL_PROMPT,
        "final_schema": DocumentAnalysisGeneration.model_json_schema(),
        "synthesis_num_predict": settings["ollama_synthesis_num_predict"],
        "user_prompt_sha256": prompt_hash,
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _context_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_page_checkpoint(
    checkpoint_store,
    entry: dict,
    *,
    job_id: str,
    page_number: int,
    total_pages: int,
    pdf_sha256: str,
    pipeline_fingerprint: str,
    context_summary: str,
) -> PageExtraction | None:
    expected_context_hash = _context_fingerprint(context_summary)
    key = entry.get("key") if isinstance(entry, dict) else None
    try:
        checkpoint, key = checkpoint_store.load_checkpoint(entry)
        expected = {
            "checkpoint_version": 1,
            "job_id": job_id,
            "page_number": page_number,
            "total_pages": total_pages,
            "pdf_sha256": pdf_sha256,
            "schema_version": PAGE_EXTRACTION_SCHEMA_VERSION,
            "pipeline_fingerprint": pipeline_fingerprint,
            "context_summary_sha256": expected_context_hash,
        }
        if not isinstance(checkpoint, dict) or any(checkpoint.get(name) != value for name, value in expected.items()):
            raise ValueError("checkpoint identity mismatch")
        extraction_data = checkpoint.get("extraction")
        if not isinstance(extraction_data, dict):
            raise ValueError("checkpoint extraction is not an object")
        extraction = PageExtraction.model_validate(extraction_data)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in {"NoSuchKey", "NoSuchObject", "404", "NotFound"}:
            raise
        extraction = None
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, ValidationError):
        extraction = None

    if extraction is None:
        log_event(
            logger,
            "CHECKPOINT_INVALID",
            level=logging.WARNING,
            processing_stage="checkpoint_load",
            job_id=job_id,
            page_number=page_number,
            total_pages=total_pages,
            checkpoint_key=key,
            schema_version=PAGE_EXTRACTION_SCHEMA_VERSION,
        )
        return None

    log_event(
        logger,
        "CHECKPOINT_LOADED",
        processing_stage="checkpoint_load",
        job_id=job_id,
        page_number=page_number,
        total_pages=total_pages,
        checkpoint_key=key,
        schema_version=PAGE_EXTRACTION_SCHEMA_VERSION,
    )
    return extraction


def _process_pdf(
    filename: str,
    contents: bytes,
    user_prompt: str | None,
    client: OllamaClient | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    *,
    job_id: str | None = None,
    checkpoint_store=None,
) -> dict:
    settings = get_settings()
    client = client or OllamaClient()
    try:
        pdf = fitz.open(stream=contents, filetype="pdf")
    except Exception as exc:
        log_event(
            logger,
            "PDF_OPEN_FAILED",
            level=logging.ERROR,
            processing_stage="pdf_open",
            error_type=type(exc).__name__,
        )
        raise ValueError("El archivo no es un PDF válido o no se pudo abrir") from exc

    page_results: list[PageExtraction] = []
    context_summary = ""
    try:
        page_count = pdf.page_count
        if page_count == 0:
            raise ValueError("El PDF no contiene páginas")
        log_event(
            logger,
            "PDF_OPENED",
            processing_stage="pdf_open",
            total_pages=page_count,
        )
        pdf_sha256 = hashlib.sha256(contents).hexdigest()
        pipeline_fingerprint = _checkpoint_fingerprint(settings) if checkpoint_store else None
        checkpoint_manifest = None
        manifest_invalid = False
        if checkpoint_store:
            try:
                checkpoint_manifest = checkpoint_store.load_manifest()
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code not in {"NoSuchKey", "NoSuchObject", "404", "NotFound"}:
                    raise
                manifest_invalid = True
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                checkpoint_manifest = None
                manifest_invalid = True

            if manifest_invalid:
                log_event(
                    logger,
                    "CHECKPOINT_INVALID",
                    level=logging.WARNING,
                    processing_stage="checkpoint_load",
                    job_id=job_id or checkpoint_store.job_id,
                    checkpoint_key=checkpoint_store.manifest_key,
                    schema_version=PAGE_EXTRACTION_SCHEMA_VERSION,
                )
                checkpoint_store.manifest = None

            expected_manifest = {
                "manifest_version": 1,
                "job_id": job_id or checkpoint_store.job_id,
                "pdf_sha256": pdf_sha256,
                "total_pages": page_count,
                "schema_version": PAGE_EXTRACTION_SCHEMA_VERSION,
                "pipeline_fingerprint": pipeline_fingerprint,
            }
            if checkpoint_manifest and (
                not isinstance(checkpoint_manifest, dict)
                or any(checkpoint_manifest.get(name) != value for name, value in expected_manifest.items())
                or not isinstance(checkpoint_manifest.get("pages"), dict)
            ):
                log_event(
                    logger,
                    "CHECKPOINT_INVALID",
                    level=logging.WARNING,
                    processing_stage="checkpoint_load",
                    job_id=job_id or checkpoint_store.job_id,
                    checkpoint_key=checkpoint_store.manifest_key,
                    schema_version=PAGE_EXTRACTION_SCHEMA_VERSION,
                )
                checkpoint_manifest = None
                checkpoint_store.manifest = None
        # Recover only a contiguous prefix. If a page is absent or invalid,
        # later pages are re-extracted so their page-context chain stays valid.
        if checkpoint_store:
            for page_index in range(page_count):
                page_number = page_index + 1
                page_entries = checkpoint_manifest.get("pages", {}) if checkpoint_manifest else {}
                if str(page_number) not in page_entries:
                    break
                extraction = _load_page_checkpoint(
                    checkpoint_store,
                    page_entries[str(page_number)],
                    job_id=job_id or checkpoint_store.job_id,
                    page_number=page_number,
                    total_pages=page_count,
                    pdf_sha256=pdf_sha256,
                    pipeline_fingerprint=pipeline_fingerprint,
                    context_summary=context_summary,
                )
                if extraction is None:
                    break
                extraction.context_summary = _bound_context(extraction.context_summary, settings["context_max_chars"])
                page_results.append(extraction)
                context_summary = extraction.context_summary
                log_event(
                    logger,
                    "PAGE_RECOVERED",
                    processing_stage="checkpoint_load",
                    job_id=job_id or checkpoint_store.job_id,
                    page_number=page_number,
                    total_pages=page_count,
                    schema_version=PAGE_EXTRACTION_SCHEMA_VERSION,
                )

        recovered_pages = len(page_results)
        if progress_callback:
            progress_callback(recovered_pages, page_count)

        first_pending_page = recovered_pages + 1
        if checkpoint_store and recovered_pages:
            log_event(
                logger,
                "PAGE_PROCESSING_RESUMED" if recovered_pages < page_count else "FINAL_SYNTHESIS_RESUMED",
                processing_stage="checkpoint_load",
                job_id=job_id or checkpoint_store.job_id,
                page_number=first_pending_page if recovered_pages < page_count else None,
                total_pages=page_count,
                recovered_pages=recovered_pages,
                schema_version=PAGE_EXTRACTION_SCHEMA_VERSION,
            )

        for page_index in range(page_count):
            page_number = page_index + 1
            if page_number <= recovered_pages:
                continue
            with diagnostic_context(processing_stage="page_render", page_number=page_number, total_pages=page_count):
                log_event(logger, "PAGE_RENDER_START", processing_stage="page_render", page_number=page_number, total_pages=page_count)
                try:
                    page = pdf.load_page(page_index)
                    raster_started = time.monotonic()
                    pixmap = page.get_pixmap(dpi=settings["render_dpi"], alpha=False)
                    image_bytes = pixmap.tobytes("png")
                    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
                    render_elapsed_ms = round((time.monotonic() - raster_started) * 1000)
                    log_event(
                        logger,
                        "PAGE_RENDER_COMPLETED",
                        processing_stage="page_render",
                        page_number=page_number,
                        total_pages=page_count,
                        image_width=pixmap.width,
                        image_height=pixmap.height,
                        image_format="PNG",
                        image_bytes=len(image_bytes),
                        image_sha256=image_sha256,
                        render_elapsed_ms=render_elapsed_ms,
                    )
                    request = (
                        f"{PAGE_PROMPT}\n\nPágina {page_number} de {page_count}.\n"
                        f"Contexto de páginas anteriores: {context_summary or 'Sin contexto previo.'}\n"
                        f"Máximo para context_summary: {settings['context_max_chars']} caracteres."
                    )
                    extraction = _generate_validated(
                        client,
                        [{"role": "user", "content": request}],
                        PageExtraction,
                        image=image_bytes,
                        page=page_number,
                        image_width=pixmap.width,
                        image_height=pixmap.height,
                        context_summary_length=len(context_summary),
                    )
                    extraction.context_summary = _bound_context(extraction.context_summary, settings["context_max_chars"])
                    if checkpoint_store:
                        checkpoint = {
                            "checkpoint_version": 1,
                            "job_id": job_id or checkpoint_store.job_id,
                            "page_number": page_number,
                            "total_pages": page_count,
                            "pdf_sha256": pdf_sha256,
                            "schema_version": PAGE_EXTRACTION_SCHEMA_VERSION,
                            "pipeline_fingerprint": pipeline_fingerprint,
                            "context_summary_sha256": _context_fingerprint(context_summary),
                            "extraction": extraction.model_dump(mode="json"),
                        }
                        checkpoint_started = time.monotonic()
                        checkpoint_key, _checkpoint_bytes = checkpoint_store.save(page_number, checkpoint)
                        log_event(
                            logger,
                            "CHECKPOINT_SAVED",
                            processing_stage="checkpoint_save",
                            job_id=job_id or checkpoint_store.job_id,
                            page_number=page_number,
                            total_pages=page_count,
                            checkpoint_key=checkpoint_key,
                            schema_version=PAGE_EXTRACTION_SCHEMA_VERSION,
                            elapsed_ms=round((time.monotonic() - checkpoint_started) * 1000),
                        )
                    page_results.append(extraction)
                    context_summary = extraction.context_summary
                    log_event(
                        logger,
                        "PAGE_COMPLETED",
                        processing_stage="page_extraction",
                        page_number=page_number,
                        total_pages=page_count,
                        pages_rendered=len(page_results),
                    )
                    if progress_callback:
                        progress_callback(page_number, page_count)
                    del image_bytes, pixmap, page
                except Exception as exc:
                    log_event(
                        logger,
                        "PAGE_PROCESSING_FAILED",
                        level=logging.ERROR,
                        processing_stage=getattr(exc, "processing_stage", None) or "page_processing",
                        page_number=getattr(exc, "page_number", None) or page_number,
                        total_pages=page_count,
                        ollama_request_id=getattr(exc, "request_id", None),
                        error_category=getattr(exc, "category", getattr(exc, "kind", "page_processing_error")),
                        error_type=type(exc).__name__,
                    )
                    raise
    finally:
        pdf.close()

    page_text = json.dumps(
        [
            {"page": page_number, **item.model_dump()}
            for page_number, item in enumerate(page_results, start=1)
        ],
        ensure_ascii=False,
    )
    final_prompt = (
        f"{FINAL_PROMPT}\n\nPetición adicional del usuario:\n{user_prompt or 'No indicada.'}\n\n"
        f"Extracciones validadas, en orden de página:\n{page_text}"
    )
    total_transcription_chars = sum(len(item.transcription) for item in page_results)
    visual_elements_count = sum(
        len(item.visual_elements.stamps) + len(item.visual_elements.signatures) + len(item.visual_elements.other_marks)
        for item in page_results
    )
    log_event(
        logger,
        "FINAL_SYNTHESIS_START",
        processing_stage="final_synthesis",
        total_pages=len(page_results),
        extraction_count=len(page_results),
        total_transcription_chars=total_transcription_chars,
        visual_elements_count=visual_elements_count,
        prompt_length=len(final_prompt),
    )
    generated = _generate_validated(
        client,
        [{"role": "user", "content": final_prompt}],
        DocumentAnalysisGeneration,
    )
    literal_transcription = "\n\n".join(
        f"--- Página {page_number} ---\n{item.transcription}"
        for page_number, item in enumerate(page_results, start=1)
    )
    final = DocumentAnalysis(
        analysis=generated.analysis,
        transcription=Transcription(text=literal_transcription),
        visual_elements=generated.visual_elements,
        requested_output=RequestedOutput(
            user_request=user_prompt or "",
            response=generated.requested_output.response if user_prompt else None,
        ),
    )
    result = {
        "filename": filename,
        "pages": len(page_results),
        # Persist page-scoped evidence in ResultsBucket so later questions can
        # retrieve/cite exact pages without reopening the source PDF or rerunning
        # PageExtraction. The legacy top-level result contract stays intact.
        "page_extractions": [
            {
                "page": page_number,
                "transcription": item.transcription,
                "analysis_notes": item.analysis_notes,
                "visual_elements": item.visual_elements.model_dump(mode="json"),
            }
            for page_number, item in enumerate(page_results, start=1)
        ],
        **final.model_dump(),
    }
    log_event(
        logger,
        "FINAL_SYNTHESIS_COMPLETED",
        processing_stage="final_synthesis",
        total_pages=len(page_results),
        extraction_count=len(page_results),
        visual_elements_count=visual_elements_count,
        outcome="valid",
    )
    return result


def process_pdf(
    filename: str,
    contents: bytes,
    user_prompt: str | None,
    client: OllamaClient | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    *,
    job_id: str | None = None,
    checkpoint_store=None,
) -> dict:
    started = time.monotonic()
    try:
        result = _process_pdf(
            filename,
            contents,
            user_prompt,
            client,
            progress_callback,
            job_id=job_id,
            checkpoint_store=checkpoint_store,
        )
        log_event(
            logger,
            "DOCUMENT_PROCESSING_COMPLETED",
            processing_stage="document",
            total_pages=result["pages"],
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return result
    except Exception as exc:
        log_event(
            logger,
            "DOCUMENT_PROCESSING_FAILED",
            level=logging.ERROR,
            processing_stage="document",
            duration_ms=round((time.monotonic() - started) * 1000),
            error_type=type(exc).__name__,
            error_category=getattr(exc, "category", getattr(exc, "kind", "processing_error")),
        )
        raise
