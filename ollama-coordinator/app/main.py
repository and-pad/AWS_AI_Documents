from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.coordinator import CoordinatorError, CoordinatorSettings, InferenceCoordinator

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger("ollama_coordinator.api")
settings = CoordinatorSettings.from_env()
coordinator = InferenceCoordinator(settings)
_instance_lock = None


def _acquire_instance_lock(path: str):
    global _instance_lock
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+", encoding="ascii")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("Another Ollama inference coordinator already owns the instance lock") from None
    _instance_lock = handle
    return handle


@asynccontextmanager
async def lifespan(_app: FastAPI):
    lock_path = os.getenv("OLLAMA_COORDINATOR_LOCK_PATH", "/run/ollama-inference-coordinator/coordinator.lock")
    lock = _acquire_instance_lock(lock_path)
    await coordinator.start()
    try:
        yield
    finally:
        await coordinator.close()
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


app = FastAPI(title="Ollama single-inference coordinator", lifespan=lifespan)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


async def _wait_for_disconnect(receive) -> None:
    """Wait for the next ASGI disconnect event after the request body is read.

    Do not poll Starlette's Request.is_disconnected() in a background task.
    That helper probes `receive()` inside an AnyIO CancelScope. Cancelling the
    polling task while that scope is active can be swallowed, leaving the
    endpoint blocked while gathering the task before it sends its response.
    The endpoint drains the complete request body first, so this task can own
    the receive channel until it sees `http.disconnect` or is cancelled.
    """
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return


def _error_response(error: CoordinatorError) -> JSONResponse:
    headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
    return JSONResponse(
        status_code=error.status_code,
        content={"error": error.code, "retryable": error.status_code in {502, 503, 504}},
        headers=headers,
    )


@app.get("/health")
async def health():
    return coordinator.health()


@app.post("/api/chat")
async def chat(request: Request):
    request_id = request.headers.get("X-Request-ID") or ""
    request_id = request_id if _REQUEST_ID_PATTERN.fullmatch(request_id) else ""
    request_id = request_id or str(uuid.uuid4())
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_request_body_bytes:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "invalid_content_length"}, status_code=400)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_request_body_bytes:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list) or not payload["messages"]:
        return JSONResponse({"error": "invalid_chat_request"}, status_code=400)
    if any(
        not isinstance(message, dict)
        or message.get("role") not in {"system", "user", "assistant"}
        or not isinstance(message.get("content"), str)
        or ("images" in message and (
            not isinstance(message["images"], list)
            or any(not isinstance(image, str) for image in message["images"])
        ))
        for message in payload["messages"]
    ):
        return JSONResponse({"error": "invalid_chat_messages"}, status_code=400)
    if payload.get("stream") is not False:
        return JSONResponse({"error": "streaming_not_supported"}, status_code=400)
    try:
        queue_wait = min(
            settings.max_queue_wait_seconds,
            max(1, int(request.headers.get("X-Coordinator-Queue-Wait-Seconds", settings.max_queue_wait_seconds))),
        )
        request_timeout = min(
            settings.max_request_seconds,
            max(1, int(request.headers.get("X-Coordinator-Request-Timeout-Seconds", settings.max_request_seconds))),
        )
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid_timeout_header"}, status_code=400)

    try:
        inference_task = asyncio.create_task(
            coordinator.infer(
                payload,
                request_id=request_id,
                queue_wait_seconds=queue_wait,
                request_timeout_seconds=request_timeout,
            )
        )
        # The request body has been fully consumed above. Read the ASGI receive
        # channel once here to detect disconnects without concurrent body reads
        # or Request.is_disconnected() polling.
        disconnect_task = asyncio.create_task(_wait_for_disconnect(request.receive))
        try:
            done, _ = await asyncio.wait(
                {inference_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done and not inference_task.done():
                inference_task.cancel()
                try:
                    await inference_task
                except asyncio.CancelledError:
                    pass
                logger.info("COORDINATOR_CLIENT_DISCONNECTED request_id=%s", request_id)
                return Response(status_code=499)
            status, content = inference_task.result()
        finally:
            if not inference_task.done():
                inference_task.cancel()
                await asyncio.gather(inference_task, return_exceptions=True)
            disconnect_task.cancel()
            await asyncio.gather(disconnect_task, return_exceptions=True)
    except CoordinatorError as exc:
        logger.warning("COORDINATOR_REQUEST_FAILED request_id=%s error_category=%s http_status=%d", request_id, exc.code, exc.status_code)
        return _error_response(exc)
    return Response(content=content, status_code=status, media_type="application/json")
