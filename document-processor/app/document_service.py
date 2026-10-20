import json
import logging
import re
import time
import hashlib
from collections.abc import Callable

import pymupdf as fitz
from pydantic import ValidationError

from app.ollama_client import OllamaClient, OllamaError
from app.schemas import (
    DocumentAnalysis,
    DocumentAnalysisGeneration,
    PageExtraction,
    RequestedOutput,
    Transcription,
)
from app.settings import get_settings
from app.diagnostics import current_job_id

logger = logging.getLogger(__name__)

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


def _log_debug_model_output(raw: str, metadata: dict) -> None:
    if not get_settings()["debug_model_output"]:
        return
    # This mode is explicitly opt-in for temporary development diagnostics.
    # Metadata is whitelisted by OllamaClient; headers and request data never reach here.
    logger.warning(
        "DEBUG_MODEL_OUTPUT raw_length=%d raw_first_500=%r raw_last_500=%r "
        "done=%r done_reason=%r eval_count=%r prompt_eval_count=%r "
        "total_duration=%r load_duration=%r prompt_eval_duration=%r eval_duration=%r",
        len(_original_model_output(raw)),
        _original_model_output(raw)[:500],
        _original_model_output(raw)[-500:],
        metadata.get("done"),
        metadata.get("done_reason"),
        metadata.get("eval_count"),
        metadata.get("prompt_eval_count"),
        metadata.get("total_duration"),
        metadata.get("load_duration"),
        metadata.get("prompt_eval_duration"),
        metadata.get("eval_duration"),
    )


def _log_structured_output_error(error: StructuredOutputError, attempt: int, raw: str) -> None:
    _log_debug_model_output(raw, _model_output_metadata(raw))
    if error.kind == "invalid_json":
        logger.warning(
            "Structured output rejected schema=%s attempt=%d kind=invalid_json "
            "error_type=JSONDecodeError position=%s parser_message=%s",
            error.schema_name,
            attempt,
            error.position,
            error.parser_message,
        )
        return

    logger.warning(
        "ValidationError %s attempt=%d error_count=%d",
        error.schema_name,
        attempt,
        len(error.errors),
    )
    for item in error.errors:
        logger.warning(
            "ValidationError %s path=%s type=%s message=%s",
            error.schema_name,
            item["path"],
            item["type"],
            item["message"],
        )


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
):
    json_schema = schema.model_json_schema()
    for attempt in range(1, MAX_STRUCTURED_OUTPUT_ATTEMPTS + 1):
        request_messages = [dict(message) for message in messages]
        call_options = {"options": {"num_ctx": get_settings()["ollama_num_ctx"]}}
        if attempt > 1:
            # Keep the image and the full extraction instructions in the same
            # user message on retry. Never include the rejected model content.
            request_messages[-1]["content"] += "\n\n" + _correction_message(last_error)
            # Keep normal sampling: temperature=0 reached a 1200-second read
            # timeout in the controlled local qwen3-vl:8b reproduction.
        started = time.monotonic()
        logger.info(
            "Model request job_id=%s page=%s schema=%s attempt=%d image_count=%d image_sha256=%s "
            "image_bytes=%d prompt_length=%d messages_count=%d num_ctx=%d",
            current_job_id(), page, schema.__name__, attempt, int(image is not None),
            hashlib.sha256(image).hexdigest()[:16] if image is not None else "none",
            len(image) if image is not None else 0,
            sum(len(message.get("content", "")) for message in request_messages), len(request_messages),
            call_options["options"]["num_ctx"],
        )
        try:
            raw = client.chat(
                request_messages,
                image=image,
                format=json_schema,
                **call_options,
            )
        except OllamaError:
            # Provider/network errors are handled by SQS; only structural output
            # failures get the bounded in-process correction attempt.
            raise
        metadata = _model_output_metadata(raw)
        logger.info(
            "Model response job_id=%s page=%s schema=%s attempt=%d elapsed=%.3f raw_length=%d content_length=%d "
            "prompt_eval_count=%s eval_count=%s done_reason=%s thinking_chars=%s",
            current_job_id(), page, schema.__name__, attempt, time.monotonic() - started,
            len(_original_model_output(raw)), len(raw),
            metadata.get("prompt_eval_count"), metadata.get("eval_count"),
            metadata.get("done_reason"), metadata.get("thinking_chars"),
        )
        try:
            return _validate_model_output(raw, schema)
        except StructuredOutputError as error:
            _log_structured_output_error(error, attempt, raw)
            if attempt == MAX_STRUCTURED_OUTPUT_ATTEMPTS:
                final_error_kind = error.kind
                break
            last_error = error

    raise OllamaError(
        f"Ollama devolvió salida {final_error_kind} inválida para {schema.__name__} "
        f"después de {MAX_STRUCTURED_OUTPUT_ATTEMPTS} intentos"
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


def _process_pdf(
    filename: str,
    contents: bytes,
    user_prompt: str | None,
    client: OllamaClient | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict:
    settings = get_settings()
    client = client or OllamaClient()
    try:
        pdf = fitz.open(stream=contents, filetype="pdf")
    except Exception as exc:
        raise ValueError("El archivo no es un PDF válido o no se pudo abrir") from exc

    page_results: list[PageExtraction] = []
    context_summary = ""
    try:
        page_count = pdf.page_count
        if page_count == 0:
            raise ValueError("El PDF no contiene páginas")
        if progress_callback:
            progress_callback(0, page_count)
        for page_index in range(page_count):
            page_number = page_index + 1
            page = pdf.load_page(page_index)
            raster_started = time.monotonic()
            pixmap = page.get_pixmap(dpi=settings["render_dpi"], alpha=False)
            image_bytes = pixmap.tobytes("png")
            logger.info(
                "Rasterized job_id=%s page=%d total_pages=%d dpi=%d image_width=%d image_height=%d "
                "image_bytes=%d image_sha256=%s elapsed=%.3f",
                current_job_id(), page_number, page_count, settings["render_dpi"], pixmap.width, pixmap.height,
                len(image_bytes), hashlib.sha256(image_bytes).hexdigest()[:16], time.monotonic() - raster_started,
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
            )
            extraction.context_summary = _bound_context(extraction.context_summary, settings["context_max_chars"])
            page_results.append(extraction)
            context_summary = extraction.context_summary
            if progress_callback:
                progress_callback(page_number, page_count)
            del image_bytes, pixmap, page
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
    logger.info(
        "Synthesis input job_id=%s page_count=%d extraction_count=%d total_transcription_chars=%d "
        "visual_elements_count=%d prompt_length=%d",
        current_job_id(), len(page_results), len(page_results),
        sum(len(item.transcription) for item in page_results),
        sum(len(item.visual_elements.stamps) + len(item.visual_elements.signatures) + len(item.visual_elements.other_marks) for item in page_results),
        len(final_prompt),
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
    return {
        "filename": filename,
        "pages": len(page_results),
        **final.model_dump(),
    }


def process_pdf(
    filename: str,
    contents: bytes,
    user_prompt: str | None,
    client: OllamaClient | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict:
    started = time.monotonic()
    try:
        result = _process_pdf(filename, contents, user_prompt, client, progress_callback)
        logger.info("Procesamiento completado filename=%s pages=%d elapsed=%.2fs", filename, result["pages"], time.monotonic() - started)
        return result
    except Exception:
        logger.exception("Procesamiento fallido filename=%s elapsed=%.2fs", filename, time.monotonic() - started)
        raise
