import asyncio
import json

from app import main


class FakeCoordinator:
    def __init__(self):
        self.calls = []

    async def start(self):
        return None

    async def close(self):
        return None

    def health(self):
        return {"status": "ok", "active_inference": False, "pending_requests": 0}

    async def infer(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        return 200, json.dumps({"model": payload["model"], "message": {"content": "{}"}, "done": True}).encode()


class FakeRequest:
    def __init__(self, payload, headers=None):
        self._body = json.dumps(payload).encode()
        self.headers = {"content-length": str(len(self._body)), **(headers or {})}

    async def stream(self):
        yield self._body

    async def is_disconnected(self):
        return False

    async def receive(self):
        await asyncio.Event().wait()


def test_api_preserves_ollama_chat_contract_and_sanitizes_request_id(monkeypatch, tmp_path):
    fake = FakeCoordinator()
    monkeypatch.setattr(main, "coordinator", fake)
    request_payload = {
        "model": "qwen3-vl:8b-instruct", "stream": False,
        "messages": [{"role": "user", "content": "test"}],
    }
    response = asyncio.run(main.chat(FakeRequest(request_payload, {"X-Request-ID": "bad\r\nid"})))
    health = asyncio.run(main.health())

    assert response.status_code == 200
    assert json.loads(response.body)["model"] == "qwen3-vl:8b-instruct"
    assert health["status"] == "ok"
    assert fake.calls[0][0] == request_payload
    assert fake.calls[0][1]["request_id"] != "bad\r\nid"
    assert fake.calls[0][1]["request_timeout_seconds"] == main.settings.max_request_seconds


def test_api_rejects_streaming_and_invalid_messages_before_dispatch(monkeypatch, tmp_path):
    fake = FakeCoordinator()
    monkeypatch.setattr(main, "coordinator", fake)
    streaming = asyncio.run(main.chat(FakeRequest({
        "model": "deepseek-r1:8b", "stream": True, "messages": [{"role": "user", "content": "x"}],
    })))
    invalid_message = asyncio.run(main.chat(FakeRequest({
        "model": "deepseek-r1:8b", "stream": False, "messages": [{"role": "user", "content": 1}],
    })))

    assert streaming.status_code == 400
    assert invalid_message.status_code == 400
    assert fake.calls == []
