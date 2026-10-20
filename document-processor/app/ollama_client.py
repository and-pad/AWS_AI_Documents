import base64
import logging
from copy import deepcopy

import requests

from app.settings import get_settings

logger = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    """Safe error raised for a failed Ollama interaction."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "internal_ollama_error",
        status_code: int | None = None,
        api_error_present: bool = False,
    ):
        self.category = category
        self.status_code = status_code
        self.api_error_present = api_error_present
        super().__init__(message)


class OllamaOutput(str):
    """Model content plus the safe response metadata needed for debug diagnostics."""

    def __new__(cls, content: str, metadata: dict | None = None, raw_content: str | None = None):
        instance = super().__new__(cls, content)
        instance.metadata = metadata or {}
        instance.raw_content = content if raw_content is None else raw_content
        return instance


_OUTPUT_METADATA_FIELDS = (
    "done",
    "done_reason",
    "eval_count",
    "prompt_eval_count",
    "total_duration",
    "load_duration",
    "prompt_eval_duration",
    "eval_duration",
)


class OllamaClient:
    def __init__(self, url: str | None = None, model: str | None = None, timeout: tuple[int, int] | None = None):
        settings = get_settings()
        self.url = (url or settings["ollama_base_url"]).rstrip("/")
        self.api_token = settings["ollama_api_token"]
        self.model = model or settings["ollama_model"]
        # A VLM page can take more than ten minutes on the on-premise GPU.
        # This is only the timeout for one Ollama page request.
        self.timeout = timeout or (10, settings["ollama_read_timeout"])

    def chat(
        self,
        messages: list[dict],
        image: bytes | None = None,
        format: dict | str | None = None,
        options: dict | None = None,
    ) -> str:
        payload_messages = deepcopy(messages)
        if image is not None:
            encoded = base64.b64encode(image).decode("ascii")
            payload_messages[-1]["images"] = [encoded]
        headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else None
        transport_failure: tuple[str, str, Exception] | None = None
        try:
            payload = {"model": self.model, "stream": False, "messages": payload_messages}
            if format is not None:
                payload["format"] = format
                # A two-pass thinking/format generation must not silently drop
                # its original image or text when the context window fills.
                payload["truncate"] = False
                payload["shift"] = False
            if options is not None:
                payload["options"] = options
            response = requests.post(
                f"{self.url}/api/chat",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.ReadTimeout as exc:
            transport_failure = ("read_timeout", "Ollama agotó el tiempo de lectura", exc)
        except requests.ConnectionError as exc:
            transport_failure = ("connection_error", "No se pudo conectar con Ollama", exc)
        except requests.RequestException as exc:
            transport_failure = ("internal_ollama_error", "Falló la solicitud HTTP a Ollama", exc)
        if transport_failure:
            category, message, cause = transport_failure
            self._raise_safe_error(category, message, cause_type=type(cause).__name__)

        http_failure: requests.HTTPError | None = None
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            http_failure = exc
        if http_failure:
            self._raise_safe_error(
                "http_error",
                "Ollama devolvió un estado HTTP de error",
                status_code=response.status_code,
                api_error_present=self._response_has_api_error(response),
                cause_type=type(http_failure).__name__,
            )

        json_failure: ValueError | None = None
        try:
            response_payload = response.json()
        except ValueError as exc:
            json_failure = exc
        if json_failure:
            self._raise_safe_error(
                "invalid_response_envelope",
                "Ollama devolvió una respuesta que no es JSON",
                status_code=response.status_code,
                cause_type=type(json_failure).__name__,
            )

        if not isinstance(response_payload, dict):
            self._raise_safe_error(
                "invalid_response_envelope",
                "Ollama devolvió un envelope JSON no válido",
                status_code=response.status_code,
            )
        if "error" in response_payload:
            self._raise_safe_error(
                "ollama_api_error",
                "Ollama devolvió un error de API",
                status_code=response.status_code,
                api_error_present=True,
            )

        message = response_payload.get("message")
        if not isinstance(message, dict):
            self._raise_safe_error(
                "invalid_response_envelope",
                "Ollama devolvió un envelope sin message válido",
                status_code=response.status_code,
            )
        if "content" not in message or message["content"] is None:
            self._raise_safe_error(
                "missing_content",
                "Ollama devolvió una respuesta sin message.content",
                status_code=response.status_code,
            )

        content = message["content"]
        if not isinstance(content, str):
            self._raise_safe_error(
                "invalid_content_type",
                "Ollama devolvió message.content con un tipo no válido",
                status_code=response.status_code,
            )
        if not content.strip():
            self._raise_safe_error(
                "missing_content",
                "Ollama devolvió message.content vacío",
                status_code=response.status_code,
            )

        metadata = {
            field: response_payload[field]
            for field in _OUTPUT_METADATA_FIELDS
            if field in response_payload
        }
        # Counts only; reasoning text never becomes result or log content.
        thinking = message.get("thinking")
        if isinstance(thinking, str):
            metadata["thinking_chars"] = len(thinking)
        # Keep existing validation behavior (trimmed content), but retain the
        # unmodified model string for the opt-in failure diagnostic.
        return OllamaOutput(content.strip(), metadata, raw_content=content)

    def _response_has_api_error(self, response: requests.Response) -> bool:
        try:
            payload = response.json()
        except ValueError:
            return False
        return isinstance(payload, dict) and "error" in payload

    def _raise_safe_error(
        self,
        category: str,
        message: str,
        *,
        status_code: int | None = None,
        api_error_present: bool = False,
        cause_type: str | None = None,
    ) -> None:
        error = OllamaError(
            message,
            category=category,
            status_code=status_code,
            api_error_present=api_error_present,
        )
        logger.error(
            "Error consultando Ollama model=%s category=%s status_code=%s api_error_present=%s",
            self.model,
            category,
            status_code,
            api_error_present,
        )
        if cause_type is None:
            raise error from None
        # Chain only a sanitized technical cause: raw requests exceptions may embed
        # URLs, headers, or provider text.
        raise error from RuntimeError(cause_type)
