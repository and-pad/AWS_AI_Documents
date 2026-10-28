import asyncio
import json

import pytest

from app.coordinator import CoordinatorError, CoordinatorSettings, InferenceCoordinator


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.content = json.dumps(payload).encode()


class FakeOllama:
    def __init__(self, gate=None):
        self.gate = gate
        self.active = 0
        self.max_active = 0
        self.models = []
        self.payloads = []

    async def post(self, _url, *, json, timeout):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.models.append(json["model"])
        self.payloads.append(json)
        try:
            if self.gate is not None:
                await self.gate.wait()
            else:
                await asyncio.sleep(0.01)
            return FakeResponse({
                "model": json["model"],
                "message": {"content": "{}"},
                "done": True,
                "done_reason": "stop",
                "load_duration": 12_000_000,
                "eval_duration": 21_000_000,
            })
        finally:
            self.active -= 1


def payload(model):
    return {"model": model, "stream": False, "messages": [{"role": "user", "content": "mock"}]}


def test_fifo_serializes_model_switches_and_only_one_inference_runs():
    async def scenario():
        upstream = FakeOllama()
        service = InferenceCoordinator(CoordinatorSettings(max_pending=4), upstream)
        await service.start()
        try:
            results = await asyncio.gather(
                service.infer(payload("qwen3-vl:8b-instruct")),
                service.infer(payload("deepseek-r1:8b")),
                service.infer(payload("qwen3-vl:8b-instruct")),
            )
            assert [status for status, _ in results] == [200, 200, 200]
            assert upstream.models == ["qwen3-vl:8b-instruct", "deepseek-r1:8b", "qwen3-vl:8b-instruct"]
            assert upstream.max_active == 1
            assert service.health()["max_active_observed"] == 1
            assert all(item["keep_alive"] == "5m" for item in upstream.payloads)
            assert upstream.payloads[0]["options"] == {"num_ctx": 16384, "num_predict": 6000}
            assert upstream.payloads[1]["options"] == {"num_ctx": 8192, "num_predict": 1000}
        finally:
            await service.close()

    asyncio.run(scenario())


def test_pending_queue_is_bounded_and_returns_recoverable_busy_response():
    async def scenario():
        gate = asyncio.Event()
        upstream = FakeOllama(gate)
        service = InferenceCoordinator(CoordinatorSettings(max_pending=1, max_queue_wait_seconds=2), upstream)
        await service.start()
        first = asyncio.create_task(service.infer(payload("qwen3-vl:8b-instruct")))
        while upstream.active == 0:
            await asyncio.sleep(0.001)
        second = asyncio.create_task(service.infer(payload("deepseek-r1:8b")))
        await asyncio.sleep(0.01)
        with pytest.raises(CoordinatorError) as error:
            await service.infer(payload("qwen3-vl:8b-instruct"))
        assert error.value.code == "coordinator_busy"
        assert error.value.status_code == 503
        assert service.health()["pending_requests"] == 1
        gate.set()
        await asyncio.gather(first, second)
        assert upstream.max_active == 1
        await service.close()

    asyncio.run(scenario())


def test_queue_wait_expiry_does_not_start_the_expired_inference():
    async def scenario():
        gate = asyncio.Event()
        upstream = FakeOllama(gate)
        service = InferenceCoordinator(CoordinatorSettings(max_pending=2, max_queue_wait_seconds=1), upstream)
        await service.start()
        first = asyncio.create_task(service.infer(payload("qwen3-vl:8b-instruct")))
        while upstream.active == 0:
            await asyncio.sleep(0.001)
        with pytest.raises(CoordinatorError) as error:
            await service.infer(payload("deepseek-r1:8b"), queue_wait_seconds=1)
        assert error.value.code == "coordinator_busy"
        gate.set()
        await first
        await asyncio.sleep(0.01)
        assert upstream.models == ["qwen3-vl:8b-instruct"]
        await service.close()

    asyncio.run(scenario())


def test_only_configured_models_are_accepted():
    async def scenario():
        service = InferenceCoordinator(http_client=FakeOllama())
        await service.start()
        try:
            with pytest.raises(CoordinatorError) as error:
                await service.infer(payload("untrusted-model"))
            assert error.value.code == "unsupported_model"
            assert error.value.status_code == 400
        finally:
            await service.close()

    asyncio.run(scenario())


def test_disconnect_cancels_upstream_before_releasing_the_slot():
    async def scenario():
        gate = asyncio.Event()
        upstream = FakeOllama(gate)
        service = InferenceCoordinator(CoordinatorSettings(max_pending=2), upstream)
        await service.start()
        first = asyncio.create_task(service.infer(payload("qwen3-vl:8b-instruct")))
        while upstream.active == 0:
            await asyncio.sleep(0.001)

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        # The caller disconnect cancels the active HTTP request; wait until
        # its task unwinds before allowing the next queued model to start.
        for _ in range(100):
            if upstream.active == 0:
                break
            await asyncio.sleep(0.001)
        assert upstream.active == 0
        gate.set()

        second = asyncio.create_task(service.infer(payload("deepseek-r1:8b")))
        await second
        assert upstream.models == ["qwen3-vl:8b-instruct", "deepseek-r1:8b"]
        assert upstream.max_active == 1
        await service.close()

    asyncio.run(scenario())


def test_health_marks_dispatcher_unavailable_outside_running_lifecycle():
    service = InferenceCoordinator(http_client=FakeOllama())
    assert service.health()["status"] == "unavailable"


def test_upstream_http_error_is_returned_as_safe_structured_error():
    class FailedOllama(FakeOllama):
        async def post(self, *_args, **_kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                return type("Response", (), {"status_code": 500, "content": b'{"error":"private upstream detail"}'})()
            finally:
                self.active -= 1

    async def scenario():
        service = InferenceCoordinator(http_client=FailedOllama())
        await service.start()
        try:
            status, body = await service.infer(payload("deepseek-r1:8b"))
            assert status == 500
            assert json.loads(body) == {"error": "ollama_upstream_error", "http_status": 500}
        finally:
            await service.close()

    asyncio.run(scenario())


def test_model_specific_memory_budgets_are_enforced_without_mutating_caller_payload():
    async def scenario():
        upstream = FakeOllama()
        service = InferenceCoordinator(http_client=upstream)
        await service.start()
        original = payload("deepseek-r1:8b")
        try:
            with pytest.raises(CoordinatorError) as too_large:
                await service.infer({**original, "options": {"num_ctx": 16384, "num_predict": 1000}})
            assert too_large.value.code == "inference_budget_exceeded"
            await service.infer(original)
            assert "options" not in original
            assert upstream.payloads[0]["options"] == {"num_ctx": 8192, "num_predict": 1000}
        finally:
            await service.close()

    asyncio.run(scenario())


def test_coordinator_shutdown_fails_active_and_pending_requests_cleanly():
    async def scenario():
        gate = asyncio.Event()
        upstream = FakeOllama(gate)
        service = InferenceCoordinator(CoordinatorSettings(max_pending=1), upstream)
        await service.start()
        active = asyncio.create_task(service.infer(payload("qwen3-vl:8b-instruct")))
        while upstream.active == 0:
            await asyncio.sleep(0.001)
        queued = asyncio.create_task(service.infer(payload("deepseek-r1:8b")))
        await asyncio.sleep(0.01)
        await service.close()
        results = await asyncio.gather(active, queued, return_exceptions=True)
        assert all(isinstance(result, CoordinatorError) for result in results)
        assert all(result.code == "coordinator_shutdown" for result in results)
        assert service.health()["status"] == "stopping"

    asyncio.run(scenario())
