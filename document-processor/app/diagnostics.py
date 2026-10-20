"""Request-local identifiers for safe model diagnostics; no document content."""
from contextlib import contextmanager
from contextvars import ContextVar

_job_id: ContextVar[str] = ContextVar('processor_job_id', default='local')


def current_job_id() -> str:
    return _job_id.get()


@contextmanager
def document_context(job_id: str):
    token = _job_id.set(job_id)
    try:
        yield
    finally:
        _job_id.reset(token)
