import os
import logging
import socket

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection

logger = logging.getLogger(__name__)


class TCPKeepAliveAdapter(HTTPAdapter):
    """Keep long-running Lambda-to-Fargate requests active across idle network timeouts."""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        options = [
            option
            for option in HTTPConnection.default_socket_options
            if option[:2] != (socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        ]
        options.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
        tcp_options = (
            ("TCP_KEEPIDLE", 60),
            ("TCP_KEEPALIVE", 60),  # macOS uses this name for TCP_KEEPIDLE.
            ("TCP_KEEPINTVL", 30),
            ("TCP_KEEPCNT", 4),
        )
        for name, value in tcp_options:
            option = getattr(socket, name, None)
            if option is not None:
                options.append((socket.IPPROTO_TCP, option, value))
        pool_kwargs["socket_options"] = options
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def _http_session() -> requests.Session:
    session = requests.Session()
    adapter = TCPKeepAliveAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def send_document(filename: str, content_type: str, content: bytes, user_prompt: str = "") -> dict:
    url = os.environ["DOCUMENT_PROCESSOR_URL"]
    try:
        with _http_session() as session:
            response = session.post(
                url,
                files={"file": (filename, content, content_type)},
                data={"user_prompt": user_prompt},
                timeout=(5, 840),
            )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        logger.exception("document-processor request failed filename=%s", filename)
        raise
