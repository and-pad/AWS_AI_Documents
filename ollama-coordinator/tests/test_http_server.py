"""HTTP-level regression tests using real local Uvicorn sockets."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app import main
from app.coordinator import CoordinatorSettings, InferenceCoordinator


class OllamaStub:
    def __init__(self, delay: float = 0.01):
        self.delay = delay
        self.started = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.app = FastAPI()

        @self.app.post("/api/chat")
        async def chat(request: Request):
            payload = await request.json()
            self.calls.append(payload)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.set()
            try:
                generation = asyncio.create_task(asyncio.sleep(self.delay))
                disconnect = asyncio.create_task(self._wait_disconnect(request))
                done, _ = await asyncio.wait(
                    {generation, disconnect}, return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect in done:
                    generation.cancel()
                    await asyncio.gather(generation, return_exceptions=True)
                    self.disconnected.set()
                    return Response(status_code=499)
                disconnect.cancel()
                await asyncio.gather(disconnect, return_exceptions=True)
                return JSONResponse({
                    "model": payload["model"],
                    "message": {"role": "assistant", "content": "{}"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                })
            finally:
                self.active -= 1

        @self.app.get("/health")
        async def health():
            return {"status": "ok"}

    async def _wait_disconnect(self, request: Request) -> None:
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                return


def _listener() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(socket.SOMAXCONN)
    sock.setblocking(False)
    return sock, sock.getsockname()[1]


async def _start_server(app: FastAPI) -> tuple[uvicorn.Server, asyncio.Task, socket.socket, int]:
    sock, port = _listener()
    server = uvicorn.Server(uvicorn.Config(
        app, log_config=None, access_log=False, lifespan="off", log_level="critical",
    ))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if server.started:
            return server, task, sock, port
        if task.done():
            await task
        await asyncio.sleep(0.01)
    raise RuntimeError("Uvicorn did not start")


@asynccontextmanager
async def _running_api(*, ollama_delay: float = 0.01, max_request_seconds: int = 1800):
    stub = OllamaStub(delay=ollama_delay)
    upstream_server, upstream_task, upstream_sock, upstream_port = await _start_server(stub.app)
    settings = CoordinatorSettings(
        upstream_url=f"http://127.0.0.1:{upstream_port}",
        max_request_seconds=max_request_seconds,
        max_queue_wait_seconds=2,
        max_pending=4,
    )
    upstream_http = httpx.AsyncClient(trust_env=False)
    coordinator = InferenceCoordinator(settings, upstream_http)
    await coordinator.start()
    original_settings, original_coordinator = main.settings, main.coordinator
    main.settings, main.coordinator = settings, coordinator
    api_server, api_task, api_sock, api_port = await _start_server(main.app)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}", timeout=4) as client:
            yield client, stub, coordinator
    finally:
        api_server.should_exit = True
        await api_task
        api_sock.close()
        await coordinator.close()
        await upstream_http.aclose()
        upstream_server.should_exit = True
        await upstream_task
        upstream_sock.close()
        main.settings, main.coordinator = original_settings, original_coordinator


def _payload(*, model="deepseek-r1:8b", content="ok", options=None):
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": content}],
    }
    if options is not None:
        payload["options"] = options
    return payload


def test_real_uvicorn_http_valid_request_returns_ollama_response():
    async def scenario():
        async with _running_api() as (client, stub, _):
            response = await client.post("/api/chat", json=_payload(options={"num_predict": 1000}))
            assert response.status_code == 200
            assert response.json()["model"] == "deepseek-r1:8b"
            assert len(stub.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (_payload(model="unknown-model"), "unsupported_model"),
        (_payload(options={"num_predict": 2000}), "inference_budget_exceeded"),
        (_payload(options={"num_ctx": 9000}), "inference_budget_exceeded"),
    ],
)
def test_real_uvicorn_http_invalid_model_and_budgets_return_immediate_400(payload, expected_error):
    async def scenario():
        async with _running_api() as (client, stub, _):
            response = await client.post("/api/chat", json=payload, timeout=1)
            assert response.status_code == 400
            assert response.json() == {"error": expected_error, "retryable": False}
            assert stub.calls == []
    asyncio.run(scenario())


def test_real_uvicorn_http_malformed_json_returns_immediate_400():
    async def scenario():
        async with _running_api() as (client, stub, _):
            response = await client.post(
                "/api/chat", content=b'{"model":', headers={"content-type": "application/json"}, timeout=1,
            )
            assert response.status_code == 400
            assert response.json() == {"error": "invalid_json"}
            assert stub.calls == []
    asyncio.run(scenario())


def test_real_uvicorn_http_disconnect_during_inference_cancels_upstream():
    async def scenario():
        async with _running_api(ollama_delay=5) as (client, stub, coordinator):
            request = asyncio.create_task(client.post("/api/chat", json=_payload()))
            await asyncio.wait_for(stub.started.wait(), timeout=1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(stub.disconnected.wait(), timeout=1)
            for _ in range(100):
                if not coordinator.health()["active_inference"]:
                    break
                await asyncio.sleep(0.01)
            assert coordinator.health()["active_inference"] is False
            assert stub.active == 0
    asyncio.run(scenario())


def test_real_uvicorn_http_inference_timeout_returns_504_and_closes_upstream():
    async def scenario():
        async with _running_api(ollama_delay=5, max_request_seconds=1) as (client, stub, coordinator):
            response = await client.post("/api/chat", json=_payload(), timeout=3)
            assert response.status_code == 504
            assert response.json() == {"error": "coordinator_request_timeout", "retryable": True}
            await asyncio.wait_for(stub.disconnected.wait(), timeout=1)
            assert coordinator.health()["active_inference"] is False
    asyncio.run(scenario())


def test_real_uvicorn_http_concurrent_requests_are_forwarded_fifo_one_at_a_time():
    async def scenario():
        async with _running_api(ollama_delay=0.15) as (client, stub, _):
            first = asyncio.create_task(client.post("/api/chat", json=_payload(content="first")))
            await asyncio.wait_for(stub.started.wait(), timeout=1)
            second = asyncio.create_task(client.post("/api/chat", json=_payload(content="second")))
            first_response, second_response = await asyncio.gather(first, second)
            assert first_response.status_code == second_response.status_code == 200
            assert [call["messages"][0]["content"] for call in stub.calls] == ["first", "second"]
            assert stub.max_active == 1
    asyncio.run(scenario())
