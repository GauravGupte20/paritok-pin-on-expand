"""The mocked upstream, served on its own loopback port.

It would be simpler to mount these routes on the main app, but then the proxies
would need to know the port the main app bound — which the app itself cannot
reliably determine. Behind a reverse proxy (Hugging Face Spaces, Render) the
inbound Host header is the public domain, and $PORT is set by some platforms and
not others. Guessing wrong fails at request time with an opaque 500.

Binding a dedicated ephemeral loopback port removes the question: the proxies are
handed an address that is correct by construction, whatever serves the UI.
"""

from __future__ import annotations

import socket
import threading
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.mock import RunRecorder, compress_reply, messages_reply


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockUpstream:
    """Runs the mock provider + compressor in a background thread."""

    def __init__(self, recorder: RunRecorder):
        self.recorder = recorder
        self.port = _free_port()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _build(self) -> Starlette:
        async def compress(request: Request) -> JSONResponse:
            return JSONResponse(compress_reply(await request.json()))

        async def messages(request: Request) -> JSONResponse:
            return JSONResponse(messages_reply(await request.json(), self.recorder))

        return Starlette(routes=[
            Route("/v1/chat/completions", compress, methods=["POST"]),
            Route("/v1/messages", messages, methods=["POST"]),
            Route("/_ping", lambda r: JSONResponse({"ok": True}), methods=["GET"]),
        ])

    def start(self, timeout: float = 15.0) -> None:
        config = uvicorn.Config(self._build(), host="127.0.0.1", port=self.port,
                                log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name="mock-upstream")
        self._thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError("mock upstream did not start in time")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
