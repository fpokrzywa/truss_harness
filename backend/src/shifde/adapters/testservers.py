"""Local, in-process protocol targets for conformance suites and tests (ephemeral ports).

* `RestTestServer`        - HTTP/JSON echo API (optional bearer auth, OpenAPI, redirects).
* `OpenAIFakeServer`      - OpenAI-compatible `/v1/models` + `/v1/chat/completions`.
* `McpTestServer`         - a real MCP server built with the official SDK (streamable HTTP).
They never echo credentials back; they only report whether the presented credential was accepted.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit


class _LocalHttpServer:
    handler: type[BaseHTTPRequestHandler]

    def __init__(self, port: int = 0) -> None:
        self._port = port  # 0 = an ephemeral port (tests); fixed for the dev sink command
        self.requests: list[dict[str, Any]] = []  # recorded (credential-free) request log
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        raise NotImplementedError

    def start(self) -> Self:
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), self._make_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _send_json(
    h: BaseHTTPRequestHandler, status: int, doc: Any, headers: dict[str, str] | None = None
) -> None:
    raw = json.dumps(doc).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(raw)))
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(raw)


def _read_json(h: BaseHTTPRequestHandler) -> Any:
    length = int(h.headers.get("Content-Length") or 0)
    raw = h.rfile.read(length) if length else b""
    return json.loads(raw) if raw else None


# =============================================================================== REST
class RestTestServer(_LocalHttpServer):
    def __init__(self, token: str | None = None, big_bytes: int = 2_000_000, port: int = 0) -> None:
        super().__init__(port)
        self.token = token
        self.big_bytes = big_bytes
        self.redirect_to = "http://169.254.169.254/latest/meta-data/"
        self.flaky_calls = 0

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:  # silence
                return

            def _authorized(self) -> bool:
                if outer.token is None:
                    return True
                got = self.headers.get("Authorization", "")
                return (
                    got == f"Bearer {outer.token}" or self.headers.get("X-API-Key") == outer.token
                )

            def _handle(self) -> None:
                # Read the whole body before any answer: closing with unread request bytes
                # sends RST, which can drop the answer before the client reads it (issue #51).
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": parts.path,
                        "query": query,
                        "authenticated": self._authorized(),
                        "idempotency_key": self.headers.get("Idempotency-Key"),
                    }
                )
                if parts.path == "/flaky":
                    outer.flaky_calls += 1
                    if outer.flaky_calls == 1:  # the first call dies without an answer
                        self.close_connection = True
                        self.connection.close()
                        return
                    _send_json(self, 200, {"ok": True, "call": outer.flaky_calls})
                    return
                if parts.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parts.path == "/big":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    sent = 0
                    chunk = b"x" * 65536
                    try:
                        while sent < outer.big_bytes:
                            self.wfile.write(chunk)
                            sent += len(chunk)
                    except OSError:
                        pass
                    return
                if parts.path == "/slow":
                    time.sleep(3)
                    _send_json(self, 200, {"slow": True})
                    return
                if not self._authorized():
                    _send_json(self, 401, {"error": "unauthorized"})
                    return
                if parts.path == "/openapi.json":
                    _send_json(
                        self,
                        200,
                        {
                            "openapi": "3.0.0",
                            "info": {"title": "test", "version": "1"},
                            "paths": {
                                "/health": {"get": {"summary": "health"}},
                                "/echo": {
                                    "get": {"summary": "echo"},
                                    "post": {"summary": "echo body"},
                                },
                            },
                        },
                    )
                    return
                if parts.path in ("/", "/health"):
                    _send_json(self, 200, {"ok": True})
                    return
                if parts.path == "/echo":
                    body = json.loads(raw) if raw and self.command != "GET" else None
                    _send_json(self, 200, {"method": self.command, "query": query, "body": body})
                    return
                _send_json(self, 404, {"error": "not found"})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle  # noqa: N815

        return Handler


# =============================================================================== OpenAI-compatible
class OpenAIFakeServer(_LocalHttpServer):
    def __init__(self, api_key: str | None = None) -> None:
        super().__init__()
        self.api_key = api_key
        self.bodies: list[dict[str, Any]] = []

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                return

            def _ok(self) -> bool:
                if outer.api_key is None:
                    return True
                return self.headers.get("Authorization") == f"Bearer {outer.api_key}"

            def do_GET(self) -> None:  # noqa: N802
                if not self._ok():
                    _send_json(self, 401, {"error": {"message": "bad key"}})
                    return
                if self.path.rstrip("/") == "/v1/models":
                    _send_json(
                        self,
                        200,
                        {
                            "object": "list",
                            "data": [
                                {"id": "fake-gpt", "object": "model"},
                                {"id": "fake-embed", "object": "model"},
                            ],
                        },
                    )
                    return
                _send_json(self, 404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:  # noqa: N802
                if not self._ok():
                    _send_json(self, 401, {"error": {"message": "bad key"}})
                    return
                if self.path.rstrip("/") != "/v1/chat/completions":
                    _send_json(self, 404, {"error": {"message": "not found"}})
                    return
                body = _read_json(self) or {}
                outer.bodies.append(body)
                model = body.get("model", "")
                if model == "error-model":
                    _send_json(self, 500, {"error": {"message": "boom"}})
                    return
                if model == "ratelimit-model":
                    _send_json(self, 429, {"error": {"message": "slow down"}})
                    return
                if model == "slow-model":
                    time.sleep(1.0)
                last = next(
                    (
                        m["content"]
                        for m in reversed(body.get("messages", []))
                        if m["role"] == "user"
                    ),
                    "",
                )
                text = f"echo: {last}"
                _send_json(
                    self,
                    200,
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": len(last.split()) + 1,
                            "completion_tokens": len(text.split()),
                            "total_tokens": 0,
                        },
                    },
                    headers={"x-request-id": "req-test-1"},
                )

        return Handler


# =============================================================================== MCP
class McpTestServer:
    """A real MCP server (official SDK, streamable HTTP) on an ephemeral loopback port."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.calls: list[str] = []
        self._server: Any = None  # Any: uvicorn.Server (optional import keeps startup light)
        self._thread: threading.Thread | None = None
        self.port = 0

    def _build_app(self) -> Callable[..., Any]:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations

        # FastMCP's logging setup is for a process without one (`shifde-admin adapters verify`);
        # WARNING, the root logger's own default, keeps the verify output quiet.
        mcp = FastMCP("shifde-test-server", log_level="WARNING")
        calls = self.calls

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def add(a: int, b: int) -> int:
            """Add two integers."""
            calls.append("add")
            return a + b

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def echo(text: str) -> str:
            """Echo text."""
            calls.append("echo")
            return text

        @mcp.tool()
        def wipe(target: str) -> str:
            """A tool with side effects (not annotated read-only)."""
            calls.append("wipe")
            return f"wiped {target}"

        seen: dict[str, int] = {}

        @mcp.tool()
        async def flaky(target: str) -> str:
            """A write whose FIRST call per target is slow (the client times out and cannot tell
            whether it landed) and whose later calls fail with an MCP `isError` result."""
            import asyncio

            calls.append("flaky")
            seen[target] = seen.get(target, 0) + 1
            if seen[target] == 1:
                await asyncio.sleep(6)
                return f"done {target}"
            raise ValueError("refused: the target is in an error state")

        app = mcp.streamable_http_app()
        token = self.token
        if token is None:
            return app

        async def guarded(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                if headers.get("authorization") != f"Bearer {token}":
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [(b"content-type", b"application/json")],
                        }
                    )
                    await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                    return
            await app(scope, receive, send)

        return guarded

    def start(self) -> Self:
        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        # The loggers stay as this process set them up (B-4098): `log_config`, `log_level` and
        # `access_log=False` would each change the process-wide uvicorn loggers for good.
        config = uvicorn.Config(self._build_app(), log_config=None, lifespan="on", ws="none")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=lambda: self._server.run(sockets=[sock]), daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("the MCP test server did not start")
            time.sleep(0.02)
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
