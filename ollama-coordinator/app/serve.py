"""Run one Uvicorn process on loopback and an optional Docker bridge IP."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket

import uvicorn

from app.main import app


def listen_hosts() -> tuple[str, ...]:
    hosts = ["127.0.0.1"]
    bridge_host = os.getenv("OLLAMA_COORDINATOR_DOCKER_BIND_HOST", "").strip()
    if bridge_host:
        try:
            address = ipaddress.ip_address(bridge_host)
        except ValueError:
            raise ValueError("OLLAMA_COORDINATOR_DOCKER_BIND_HOST must be a literal private IPv4 address") from None
        if address.version != 4 or address.is_loopback or address.is_unspecified or not address.is_private:
            raise ValueError("OLLAMA_COORDINATOR_DOCKER_BIND_HOST must be a private, non-loopback IPv4 address")
        if str(address) not in hosts:
            hosts.append(str(address))
    return tuple(hosts)


def create_listening_sockets(hosts: tuple[str, ...], port: int) -> list[socket.socket]:
    sockets: list[socket.socket] = []
    try:
        for host in hosts:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            sockets.append(listener)
        return sockets
    except Exception:
        for listener in sockets:
            listener.close()
        raise


async def _serve() -> None:
    port = int(os.getenv("OLLAMA_COORDINATOR_PORT", "11435"))
    if not 1 <= port <= 65535:
        raise ValueError("OLLAMA_COORDINATOR_PORT must be between 1 and 65535")
    listeners = create_listening_sockets(listen_hosts(), port)
    config = uvicorn.Config(app, log_config=None, access_log=False, workers=1)
    try:
        await uvicorn.Server(config).serve(sockets=listeners)
    finally:
        for listener in listeners:
            listener.close()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
