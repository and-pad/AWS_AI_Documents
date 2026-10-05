import os
from functools import lru_cache


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


@lru_cache
def get_settings() -> dict:
    _load_dotenv()
    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173")
    return {
        "ollama_url": os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        "ollama_model": os.getenv("OLLAMA_MODEL", "qwen3-vl:8b"),
        "cors_origins": [origin.strip() for origin in origins.split(",") if origin.strip()],
        "max_upload_bytes": int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
        "render_dpi": int(os.getenv("PDF_RENDER_DPI", "180")),
        "context_max_chars": int(os.getenv("CONTEXT_MAX_CHARS", "3000")),
    }
