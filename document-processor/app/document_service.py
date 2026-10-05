import json
import logging
import time

import pymupdf as fitz

from app.ollama_client import OllamaClient
from app.settings import get_settings

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """Eres un sistema de análisis documental.

Analiza cuidadosamente la imagen del documento proporcionado.

Tu objetivo es identificar y extraer información visible en el documento.

Debes:

1. Transcribir el texto que sea legible.
2. Identificar el tipo de documento cuando sea posible.
3. Identificar encabezados y secciones.
4. Identificar tablas y describir su contenido.
5. Identificar fechas.
6. Identificar nombres de personas u organizaciones.
7. Identificar firmas.
8. Identificar sellos o estampas.
9. Identificar texto manuscrito.
10. Describir otros elementos visuales relevantes.

No inventes información.

Si una parte del documento no es legible, indícalo explícitamente.

Distingue entre información que puedes leer directamente y conclusiones que solamente puedes inferir.

Devuelve el resultado en español."""


def _parse_page_response(raw: str, prior_context: str, max_chars: int) -> tuple[str, str]:
    """Accept structured model output, with a safe bounded fallback for plain text."""
    def bound(value: str) -> str:
        value = value.strip()
        if len(value) <= max_chars:
            return value
        if max_chars < 32:
            return value[:max_chars]
        marker = "\n… contexto intermedio omitido …\n"
        half = (max_chars - len(marker)) // 2
        return value[:half] + marker + value[-half:]

    try:
        data = json.loads(raw)
        analysis = data.get("analysis")
        summary = data.get("context_summary")
        if isinstance(analysis, str) and analysis.strip():
            if isinstance(summary, str):
                return analysis.strip(), bound(summary)
            return analysis.strip(), bound(prior_context + "\n" + analysis)
    except (json.JSONDecodeError, AttributeError):
        pass
    return raw.strip(), bound(prior_context + "\n" + raw.strip())


def _process_pdf(filename: str, contents: bytes, user_prompt: str | None, client: OllamaClient | None = None) -> dict:
    settings = get_settings()
    client = client or OllamaClient()
    try:
        pdf = fitz.open(stream=contents, filetype="pdf")
    except Exception as exc:
        raise ValueError("El archivo no es un PDF válido o no se pudo abrir") from exc

    page_results: list[dict] = []
    context_summary = ""
    try:
        page_count = pdf.page_count
        if page_count == 0:
            raise ValueError("El PDF no contiene páginas")
        for page_index in range(page_count):
            page_number = page_index + 1
            page = pdf.load_page(page_index)
            pixmap = page.get_pixmap(dpi=settings["render_dpi"], alpha=False)
            image_bytes = pixmap.tobytes("png")
            logger.info("Procesando página %d/%d filename=%s", page_number, page_count, filename)
            request = (
                f"{ANALYSIS_PROMPT}\n\nPágina {page_number} de {page_count}.\n"
                f"Contexto resumido de páginas anteriores (puede estar vacío):\n{context_summary or 'Sin contexto previo.'}\n\n"
                f"Instrucción adicional del usuario (opcional; no sustituye el análisis base):\n{user_prompt or 'Ninguna.'}\n\n"
                "Realiza el análisis de esta página. Devuelve solo un objeto JSON con dos cadenas: "
                '"analysis" (análisis detallado de esta página) y "context_summary" (resumen actualizado, '
                f"solo lo necesario para interpretar páginas siguientes; máximo {settings['context_max_chars']} caracteres)."
            )
            raw = client.chat([{"role": "user", "content": request}], image=image_bytes)
            analysis, context_summary = _parse_page_response(raw, context_summary, settings["context_max_chars"])
            page_results.append({"page": page_number, "analysis": analysis})
            del image_bytes, pixmap, page
    finally:
        pdf.close()

    page_text = "\n\n".join(f"Página {item['page']}:\n{item['analysis']}" for item in page_results)
    final_prompt = (
        "Consolida el análisis del documento completo usando únicamente la información extraída del documento. "
        "No inventes datos. Distingue los hechos visibles de las inferencias. Responde en español. "
        "Considera todas las páginas y cumple el objetivo específico del usuario cuando exista. Si el usuario "
        "solicita redactar una respuesta, redacta una propuesta basada exclusivamente en la información disponible.\n\n"
        f"Objetivo específico del usuario (opcional): {user_prompt or 'No indicado.'}\n\n"
        f"Análisis por página:\n{page_text}"
    )
    final_analysis = client.chat([{"role": "user", "content": final_prompt}])
    return {
        "filename": filename,
        "pages": len(page_results),
        "page_results": page_results,
        "final_analysis": final_analysis,
        "user_prompt": user_prompt or "",
    }


def process_pdf(filename: str, contents: bytes, user_prompt: str | None, client: OllamaClient | None = None) -> dict:
    started = time.monotonic()
    try:
        result = _process_pdf(filename, contents, user_prompt, client)
        logger.info("Procesamiento completado filename=%s pages=%d elapsed=%.2fs", filename, result["pages"], time.monotonic() - started)
        return result
    except Exception:
        logger.exception("Procesamiento fallido filename=%s elapsed=%.2fs", filename, time.monotonic() - started)
        raise
