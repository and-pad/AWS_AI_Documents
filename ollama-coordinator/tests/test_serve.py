import pytest

from app.serve import listen_hosts


def test_coordinator_listens_on_loopback_and_optional_docker_private_bridge(monkeypatch):
    monkeypatch.delenv("OLLAMA_COORDINATOR_DOCKER_BIND_HOST", raising=False)
    assert listen_hosts() == ("127.0.0.1",)

    monkeypatch.setenv("OLLAMA_COORDINATOR_DOCKER_BIND_HOST", "172.17.0.1")
    assert listen_hosts() == ("127.0.0.1", "172.17.0.1")


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.2", "8.8.8.8", "not-an-ip", "2001:db8::1"])
def test_coordinator_rejects_non_private_or_wildcard_bind_addresses(monkeypatch, host):
    monkeypatch.setenv("OLLAMA_COORDINATOR_DOCKER_BIND_HOST", host)
    with pytest.raises(ValueError):
        listen_hosts()
