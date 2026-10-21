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


def _positive_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        parsed = default if value is None else int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.getenv(name)
    try:
        parsed = default if value is None else int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}") from None
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return parsed


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
        "ollama_read_timeout": _positive_int_env("OLLAMA_READ_TIMEOUT", 1800),
        # Image tokens, schema and reasoning exceeded the local 4096-token context.
        "ollama_num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "16384")),
        "ollama_page_num_predict": _positive_int_env("OLLAMA_PAGE_NUM_PREDICT", 6000),
        "ollama_synthesis_num_predict": _positive_int_env("OLLAMA_SYNTHESIS_NUM_PREDICT", 2000),
        "document_queue_url": os.getenv("DOCUMENT_QUEUE_URL", ""),
        "documents_bucket": os.getenv("DOCUMENTS_BUCKET", ""),
        "results_bucket": os.getenv("RESULTS_BUCKET", ""),
        "jobs_table": os.getenv("JOBS_TABLE", ""),
        "sqs_wait_time_seconds": int(os.getenv("SQS_WAIT_TIME_SECONDS", "20")),
        "sqs_visibility_timeout": int(os.getenv("SQS_VISIBILITY_TIMEOUT", "900")),
        "sqs_visibility_heartbeat_seconds": int(os.getenv("SQS_VISIBILITY_HEARTBEAT_SECONDS", "300")),
        "sqs_max_receive_count": int(os.getenv("SQS_MAX_RECEIVE_COUNT", "3")),
        # One is the safe rollout default; two independent message consumers
        # can be enabled explicitly after validating Ollama capacity.
        "processor_max_concurrent_jobs": _bounded_int_env(
            "PROCESSOR_MAX_CONCURRENT_JOBS", 1, minimum=1, maximum=2
        ),
    }
