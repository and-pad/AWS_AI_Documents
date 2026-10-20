import socket

from app import ai_client


def test_processor_request_enables_tcp_keepalive_before_idle_connection_expiry():
    adapter = ai_client.TCPKeepAliveAdapter()
    options = adapter.poolmanager.connection_pool_kw["socket_options"]
    configured = {(level, option): value for level, option, value in options}

    assert configured[(socket.SOL_SOCKET, socket.SO_KEEPALIVE)] == 1
    idle_option = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", None))
    assert idle_option is not None
    assert configured[(socket.IPPROTO_TCP, idle_option)] == 60
    assert configured[(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)] == 30
    assert configured[(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)] == 4
    adapter.close()


def test_send_document_uses_keepalive_session_and_preserves_multipart_request(monkeypatch):
    monkeypatch.setenv("DOCUMENT_PROCESSOR_URL", "http://document-processor.dev.internal:8000/process")
    calls = {}

    class Response:
        def raise_for_status(self):
            calls["status_checked"] = True

        def json(self):
            return {"analysis": {"summary": "ok"}}

    class Session:
        def __init__(self):
            self.adapters = {}

        def mount(self, prefix, adapter):
            self.adapters[prefix] = adapter

        def post(self, url, **kwargs):
            calls.update({"url": url, **kwargs})
            calls["adapters"] = self.adapters
            return Response()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(ai_client.requests, "Session", Session)

    result = ai_client.send_document("report.pdf", "application/pdf", b"pdf", "extraer fechas")

    assert result == {"analysis": {"summary": "ok"}}
    assert calls["url"] == "http://document-processor.dev.internal:8000/process"
    assert calls["files"] == {"file": ("report.pdf", b"pdf", "application/pdf")}
    assert calls["data"] == {"user_prompt": "extraer fechas"}
    assert calls["timeout"] == (5, 840)
    assert calls["status_checked"] is True
    assert isinstance(calls["adapters"]["http://"], ai_client.TCPKeepAliveAdapter)
    assert isinstance(calls["adapters"]["https://"], ai_client.TCPKeepAliveAdapter)
