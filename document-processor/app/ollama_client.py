import base64
import logging
import time

import requests

from app.settings import get_settings

logger = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, url: str | None = None, model: str | None = None, timeout: tuple[int, int] = (10, 600)):
        settings = get_settings()
        self.url = (url or settings["ollama_url"]).rstrip("/")
        self.model = model or settings["ollama_model"]
        self.timeout = timeout

    def chat(self, messages: list[dict], image: bytes | None = None) -> str:
        payload_messages = [dict(message) for message in messages]
        if image is not None:
            encoded = base64.b64encode(image).decode("ascii")
            payload_messages[-1]["images"] = [encoded]
        started = time.monotonic()
        try:
            response = requests.post(
                f"{self.url}/api/chat",
                json={"model": self.model, "stream": False, "messages": payload_messages},
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise OllamaError("Ollama devolvió una respuesta vacía o inválida")
            return content.strip()
        except (requests.RequestException, ValueError, OllamaError) as exc:
            logger.exception("Error consultando Ollama model=%s elapsed=%.2fs", self.model, time.monotonic() - started)
            if isinstance(exc, OllamaError):
                raise
            raise OllamaError(f"No se pudo procesar la solicitud con Ollama: {exc}") from exc

