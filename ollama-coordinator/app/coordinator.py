from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("ollama_coordinator")


@dataclass(frozen=True)
class CoordinatorSettings:
    upstream_url: str = "http://127.0.0.1:11434"
    document_model: str = "qwen3-vl:8b-instruct"
    ask_model: str = "deepseek-r1:8b"
    document_max_num_ctx: int = 16384
    document_max_num_predict: int = 6000
    ask_max_num_ctx: int = 8192
    ask_max_num_predict: int = 1000
    max_pending: int = 2
    max_request_body_bytes: int = 64 * 1024 * 1024
    max_response_body_bytes: int = 16 * 1024 * 1024
    max_queue_wait_seconds: int = 120
    max_request_seconds: int = 1800
    keep_alive: str = "5m"

    @classmethod
    def from_env(cls) -> "CoordinatorSettings":
        import os

        def positive(name: str, default: int, maximum: int) -> int:
            raw = os.getenv(name, str(default))
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be an integer") from None
            if value < 1 or value > maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
            return value

        return cls(
            upstream_url=os.getenv("OLLAMA_COORDINATOR_UPSTREAM_URL", "http://127.0.0.1:11434").rstrip("/"),
            document_model=os.getenv("OLLAMA_COORDINATOR_DOCUMENT_MODEL", "qwen3-vl:8b-instruct"),
            ask_model=os.getenv("OLLAMA_COORDINATOR_ASK_MODEL", "deepseek-r1:8b"),
            document_max_num_ctx=positive("OLLAMA_COORDINATOR_DOCUMENT_MAX_NUM_CTX", 16384, 65536),
            document_max_num_predict=positive("OLLAMA_COORDINATOR_DOCUMENT_MAX_NUM_PREDICT", 6000, 32768),
            ask_max_num_ctx=positive("OLLAMA_COORDINATOR_ASK_MAX_NUM_CTX", 8192, 65536),
            ask_max_num_predict=positive("OLLAMA_COORDINATOR_ASK_MAX_NUM_PREDICT", 1000, 32768),
            max_pending=positive("OLLAMA_COORDINATOR_MAX_PENDING", 2, 32),
            max_request_body_bytes=positive("OLLAMA_COORDINATOR_MAX_BODY_BYTES", 64 * 1024 * 1024, 128 * 1024 * 1024),
            max_response_body_bytes=positive("OLLAMA_COORDINATOR_MAX_RESPONSE_BYTES", 16 * 1024 * 1024, 64 * 1024 * 1024),
            max_queue_wait_seconds=positive("OLLAMA_COORDINATOR_MAX_QUEUE_WAIT_SECONDS", 120, 600),
            max_request_seconds=positive("OLLAMA_COORDINATOR_MAX_REQUEST_SECONDS", 1800, 1800),
            keep_alive=os.getenv("OLLAMA_COORDINATOR_KEEP_ALIVE", "5m"),
        )

    def allowed_models(self) -> frozenset[str]:
        return frozenset({self.document_model, self.ask_model})


class CoordinatorError(RuntimeError):
    def __init__(self, code: str, status_code: int, retry_after: int = 0):
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(code)


@dataclass
class _Work:
    request_id: str
    model: str
    payload: dict[str, Any]
    enqueued_at: float
    queue_wait_seconds: int
    request_timeout_seconds: int
    future: asyncio.Future[tuple[int, bytes]]
    started: asyncio.Event
    cancelled: asyncio.Event
    active: bool = False


class InferenceCoordinator:
    """FIFO dispatcher with exactly one upstream /api/chat request in flight."""

    def __init__(self, settings: CoordinatorSettings | None = None, http_client: Any = None):
        self.settings = settings or CoordinatorSettings.from_env()
        if self.settings.document_model == self.settings.ask_model:
            raise ValueError("The document and question models must be distinct")
        self._queue: asyncio.Queue[_Work] = asyncio.Queue(maxsize=self.settings.max_pending)
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._worker_task: asyncio.Task | None = None
        self._stopping = False
        self._active: _Work | None = None
        self._current_model: str | None = None
        self._max_active_observed = 0
        self._completed = 0
        self._failed = 0
        self._last_queue_wait_ms: int | None = None
        self._last_inference_ms: int | None = None

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        if self._owns_http_client:
            self._http_client = httpx.AsyncClient(trust_env=False)
        self._stopping = False
        self._worker_task = asyncio.create_task(self._dispatch(), name="ollama-inference-dispatcher")

    async def close(self) -> None:
        self._stopping = True
        if self._active is not None:
            self._active.cancelled.set()
            if not self._active.future.done():
                self._active.future.set_exception(CoordinatorError("coordinator_shutdown", 503, 60))
        while not self._queue.empty():
            work = self._queue.get_nowait()
            work.cancelled.set()
            if not work.future.done():
                work.future.set_exception(CoordinatorError("coordinator_shutdown", 503, 60))
            self._queue.task_done()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def health(self) -> dict[str, Any]:
        dispatcher_ready = self._worker_task is not None and not self._worker_task.done()
        return {
            "status": "stopping" if self._stopping else "ok" if dispatcher_ready else "unavailable",
            "dispatcher_ready": dispatcher_ready,
            "active_inference": self._active is not None,
            "active_model": self._current_model if self._active is not None else None,
            "pending_requests": self._queue.qsize(),
            "max_pending_requests": self.settings.max_pending,
            "max_active_observed": self._max_active_observed,
            "completed_requests": self._completed,
            "failed_requests": self._failed,
            "last_queue_wait_ms": self._last_queue_wait_ms,
            "last_inference_ms": self._last_inference_ms,
        }

    async def infer(
        self,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        queue_wait_seconds: int | None = None,
        request_timeout_seconds: int | None = None,
    ) -> tuple[int, bytes]:
        if self._stopping or self._worker_task is None or self._worker_task.done():
            raise CoordinatorError("coordinator_unavailable", 503, 60)
        model = payload.get("model")
        if not isinstance(model, str) or model not in self.settings.allowed_models():
            raise CoordinatorError("unsupported_model", 400)
        if payload.get("stream") is not False:
            raise CoordinatorError("streaming_not_supported", 400)

        body = dict(payload)
        body["model"] = model
        raw_options = payload.get("options", {})
        if not isinstance(raw_options, dict):
            raise CoordinatorError("invalid_inference_options", 400)
        options = dict(raw_options)
        max_ctx, max_predict = (
            (self.settings.document_max_num_ctx, self.settings.document_max_num_predict)
            if model == self.settings.document_model
            else (self.settings.ask_max_num_ctx, self.settings.ask_max_num_predict)
        )
        for option, maximum in (("num_ctx", max_ctx), ("num_predict", max_predict)):
            value = options.get(option, maximum)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise CoordinatorError("invalid_inference_options", 400)
            if value > maximum:
                raise CoordinatorError("inference_budget_exceeded", 400)
            options[option] = value
        body["options"] = options
        # A bounded keep-alive avoids repeated loads for consecutive requests
        # while allowing Ollama's MAX_LOADED_MODELS=1 scheduler to evict the
        # previous idle model on a model switch.
        body["keep_alive"] = self.settings.keep_alive
        now = time.monotonic()
        loop = asyncio.get_running_loop()
        work = _Work(
            request_id=request_id or str(uuid.uuid4()),
            model=model,
            payload=body,
            enqueued_at=now,
            queue_wait_seconds=min(
                self.settings.max_queue_wait_seconds,
                max(1, queue_wait_seconds or self.settings.max_queue_wait_seconds),
            ),
            request_timeout_seconds=min(
                self.settings.max_request_seconds,
                max(1, request_timeout_seconds or self.settings.max_request_seconds),
            ),
            future=loop.create_future(),
            started=asyncio.Event(),
            cancelled=asyncio.Event(),
        )
        try:
            self._queue.put_nowait(work)
        except asyncio.QueueFull:
            logger.warning("INFERENCE_REJECTED request_id=%s model=%s reason=queue_full", work.request_id, model)
            raise CoordinatorError("coordinator_busy", 503, self.settings.max_queue_wait_seconds) from None

        logger.info("INFERENCE_QUEUED request_id=%s model=%s pending_requests=%d", work.request_id, model, self._queue.qsize())
        overall_remaining = work.request_timeout_seconds
        # Queue wait is separately bounded so callers with short execution
        # limits receive a retryable response instead of occupying Lambda.
        started_task = asyncio.create_task(work.started.wait())
        try:
            done, _ = await asyncio.wait(
                {work.future, started_task},
                timeout=min(work.queue_wait_seconds, overall_remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            work.cancelled.set()
            if not work.future.done():
                work.future.cancel()
            started_task.cancel()
            logger.info("INFERENCE_CANCELLED request_id=%s model=%s", work.request_id, model)
            raise
        if work.future in done:
            started_task.cancel()
            return work.future.result()
        if not work.started.is_set():
            started_task.cancel()
            work.cancelled.set()
            if not work.future.done():
                work.future.cancel()
            logger.warning("INFERENCE_REJECTED request_id=%s model=%s reason=queue_wait_timeout", work.request_id, model)
            raise CoordinatorError("coordinator_busy", 503, self.settings.max_queue_wait_seconds)

        remaining = work.request_timeout_seconds - (time.monotonic() - work.enqueued_at)
        if remaining <= 0:
            work.cancelled.set()
            if not work.future.done():
                work.future.cancel()
            raise CoordinatorError("coordinator_request_timeout", 504, self.settings.max_queue_wait_seconds)
        try:
            return await asyncio.wait_for(asyncio.shield(work.future), timeout=remaining)
        except asyncio.TimeoutError:
            work.cancelled.set()
            if not work.future.done():
                work.future.cancel()
            logger.warning("INFERENCE_TIMEOUT request_id=%s model=%s", work.request_id, model)
            raise CoordinatorError("coordinator_request_timeout", 504, self.settings.max_queue_wait_seconds) from None
        except asyncio.CancelledError:
            work.cancelled.set()
            if not work.future.done():
                work.future.cancel()
            logger.info("INFERENCE_CANCELLED request_id=%s model=%s", work.request_id, model)
            raise

    async def _dispatch(self) -> None:
        while not self._stopping:
            work = await self._queue.get()
            try:
                if work.cancelled.is_set() or work.future.cancelled():
                    continue
                queue_wait_ms = round((time.monotonic() - work.enqueued_at) * 1000)
                if queue_wait_ms > work.queue_wait_seconds * 1000:
                    if not work.future.done():
                        work.future.set_exception(CoordinatorError("coordinator_busy", 503, self.settings.max_queue_wait_seconds))
                    continue
                if self._active is not None:
                    # Defensive invariant: this cannot happen while one dispatch
                    # task owns this loop; fail closed if code changes later.
                    if not work.future.done():
                        work.future.set_exception(CoordinatorError("coordinator_invariant_violation", 503, 60))
                    self._failed += 1
                    continue

                work.active = True
                self._active = work
                self._current_model = work.model
                self._max_active_observed = max(self._max_active_observed, 1)
                self._last_queue_wait_ms = queue_wait_ms
                work.started.set()
                logger.info(
                    "INFERENCE_STARTED request_id=%s model=%s queue_wait_ms=%d pending_requests=%d",
                    work.request_id, work.model, queue_wait_ms, self._queue.qsize(),
                )
                inference_started = time.monotonic()
                remaining = work.request_timeout_seconds - (time.monotonic() - work.enqueued_at)
                timeout = httpx.Timeout(max(0.1, remaining), connect=min(5.0, max(0.1, remaining)))
                try:
                    status, response_body = await self._post_until_cancelled(
                        work, timeout, timeout_seconds=max(0.1, remaining),
                    )
                except CoordinatorError as exc:
                    self._failed += 1
                    if not work.future.done():
                        work.future.set_exception(exc)
                    if exc.code == "client_cancelled":
                        logger.info("INFERENCE_UPSTREAM_CANCELLED request_id=%s model=%s", work.request_id, work.model)
                    else:
                        logger.warning("INFERENCE_FAILED request_id=%s model=%s error_category=%s",
                                       work.request_id, work.model, exc.code)
                    continue
                except (httpx.TimeoutException, asyncio.TimeoutError):
                    self._failed += 1
                    if not work.future.done():
                        work.future.set_exception(CoordinatorError("ollama_timeout", 504, 60))
                    logger.warning("INFERENCE_FAILED request_id=%s model=%s error_category=upstream_timeout", work.request_id, work.model)
                    continue
                except httpx.HTTPError as exc:
                    self._failed += 1
                    if not work.future.done():
                        work.future.set_exception(CoordinatorError("ollama_connection_error", 502, 60))
                    logger.warning(
                        "INFERENCE_FAILED request_id=%s model=%s error_category=%s",
                        work.request_id, work.model, type(exc).__name__,
                    )
                    continue
                except Exception as exc:
                    self._failed += 1
                    if not work.future.done():
                        work.future.set_exception(CoordinatorError("coordinator_internal_error", 502, 60))
                    logger.error(
                        "INFERENCE_FAILED request_id=%s model=%s error_category=internal_%s",
                        work.request_id, work.model, type(exc).__name__,
                    )
                    continue

                elapsed_ms = round((time.monotonic() - inference_started) * 1000)
                self._last_inference_ms = elapsed_ms
                status = int(status)
                if status < 200 or status >= 300:
                    self._failed += 1
                    if status in {408, 429, 500, 502, 503, 504}:
                        error_body = {"error": "ollama_upstream_error", "http_status": status}
                    else:
                        error_body = {"error": "ollama_request_rejected", "http_status": status}
                    response = (status, json.dumps(error_body).encode())
                    if not work.future.done():
                        work.future.set_result(response)
                    logger.warning("INFERENCE_FAILED request_id=%s model=%s http_status=%d elapsed_ms=%d", work.request_id, work.model, status, elapsed_ms)
                    continue
                try:
                    decoded = json.loads(response_body)
                except (TypeError, ValueError):
                    self._failed += 1
                    if not work.future.done():
                        work.future.set_exception(CoordinatorError("ollama_invalid_response", 502, 60))
                    logger.warning("INFERENCE_FAILED request_id=%s model=%s error_category=invalid_json", work.request_id, work.model)
                    continue
                if not isinstance(decoded, dict):
                    self._failed += 1
                    if not work.future.done():
                        work.future.set_exception(CoordinatorError("ollama_invalid_response", 502, 60))
                    logger.warning("INFERENCE_FAILED request_id=%s model=%s error_category=invalid_envelope", work.request_id, work.model)
                    continue
                if not work.future.done():
                    work.future.set_result((status, response_body))
                self._completed += 1
                model_reported = decoded.get("model") if isinstance(decoded.get("model"), str) else None
                load_ns = decoded.get("load_duration") if isinstance(decoded.get("load_duration"), int) else None
                eval_ns = decoded.get("eval_duration") if isinstance(decoded.get("eval_duration"), int) else None
                logger.info(
                    "INFERENCE_COMPLETED request_id=%s model=%s reported_model=%s http_status=%d queue_wait_ms=%d elapsed_ms=%d load_duration_ms=%s eval_duration_ms=%s client_disconnected=%s",
                    work.request_id, work.model, model_reported, status, queue_wait_ms, elapsed_ms,
                    round(load_ns / 1_000_000) if load_ns is not None else None,
                    round(eval_ns / 1_000_000) if eval_ns is not None else None,
                    work.future.cancelled(),
                )
            finally:
                if self._active is work:
                    self._active = None
                    self._current_model = None
                self._queue.task_done()

    async def _post_until_cancelled(
        self,
        work: _Work,
        timeout: httpx.Timeout,
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        """Abort the upstream HTTP request when the caller or service stops waiting.

        Closing the request lets Ollama observe the disconnect and cancel work
        where supported. The dispatcher retains its single slot until the
        HTTP task has actually unwound, so another model is never started on
        top of a still-open coordinator request.
        """
        upstream_task = asyncio.create_task(self._post_bounded(work, timeout))
        cancel_task = asyncio.create_task(work.cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {upstream_task, cancel_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if upstream_task in done:
                return upstream_task.result()
            upstream_task.cancel()
            await asyncio.gather(upstream_task, return_exceptions=True)
            if cancel_task in done and work.cancelled.is_set():
                raise CoordinatorError("client_cancelled", 499)
            raise asyncio.TimeoutError
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            if not upstream_task.done():
                upstream_task.cancel()
                await asyncio.gather(upstream_task, return_exceptions=True)

    async def _post_bounded(self, work: _Work, timeout: httpx.Timeout) -> tuple[int, bytes]:
        if not callable(getattr(self._http_client, "stream", None)):
            response = await self._http_client.post(
                f"{self.settings.upstream_url}/api/chat", json=work.payload, timeout=timeout,
            )
            body = bytes(response.content)
            if len(body) > self.settings.max_response_body_bytes:
                raise CoordinatorError("ollama_response_too_large", 502, 60)
            return int(response.status_code), body
        body = bytearray()
        async with self._http_client.stream(
            "POST",
            f"{self.settings.upstream_url}/api/chat",
            json=work.payload,
            timeout=timeout,
        ) as response:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self.settings.max_response_body_bytes:
                    raise CoordinatorError("ollama_response_too_large", 502, 60)
            return int(response.status_code), bytes(body)
