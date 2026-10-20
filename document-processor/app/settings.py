import os
from functools import lru_cache


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache
def get_settings() -> dict:
    _load_dotenv()
    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173")
    ollama_base_url = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_URL", "http://localhost:11434")
    return {
        # OLLAMA_URL remains a compatibility alias for existing local .env files.
        "ollama_base_url": ollama_base_url.rstrip("/"),
        "ollama_url": ollama_base_url.rstrip("/"),
        "ollama_api_token": os.getenv("OLLAMA_API_TOKEN") or "",
        "ollama_model": os.getenv("OLLAMA_MODEL", "qwen3-vl:8b-instruct"),
        "cors_origins": [origin.strip() for origin in origins.split(",") if origin.strip()],
        "max_upload_bytes": int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
        "render_dpi": int(os.getenv("PDF_RENDER_DPI", "180")),
        "context_max_chars": int(os.getenv("CONTEXT_MAX_CHARS", "3000")),
        "debug_model_output": _env_bool("DEBUG_MODEL_OUTPUT"),
        "ollama_read_timeout": int(os.getenv("OLLAMA_READ_TIMEOUT", "1200")),
        # Image tokens, schema and reasoning exceeded the local 4096-token context.
        "ollama_num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "16384")),
        "document_queue_url": os.getenv("DOCUMENT_QUEUE_URL", ""),
        "documents_bucket": os.getenv("DOCUMENTS_BUCKET", ""),
        "results_bucket": os.getenv("RESULTS_BUCKET", ""),
        "jobs_table": os.getenv("JOBS_TABLE", ""),
        "sqs_wait_time_seconds": int(os.getenv("SQS_WAIT_TIME_SECONDS", "20")),
        "sqs_visibility_timeout": int(os.getenv("SQS_VISIBILITY_TIMEOUT", "900")),
        "sqs_visibility_heartbeat_seconds": int(os.getenv("SQS_VISIBILITY_HEARTBEAT_SECONDS", "300")),
        "sqs_max_receive_count": int(os.getenv("SQS_MAX_RECEIVE_COUNT", "3")),
    }
