"""Global request-body cap (audit Item 5b / REQ F-15): a pure ASGI middleware, so it also bounds
bodies that arrive without a Content-Length (chunked) and never buffers more than the cap."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

PROBLEM_MEDIA_TYPE = "application/problem+json"


class _TooLarge(Exception):
    pass


class MaxBodySizeMiddleware:
    """Answer 413 (problem+json, code `payload_too_large`) when a request body exceeds
    `max_bytes`; `max_bytes <= 0` disables the cap."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0  # malformed: the byte counter below still enforces the cap
                if declared > self.max_bytes:
                    await self._reject(send, scope)
                    return
        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if started:
                raise  # the response is already on the wire; the server closes the connection
            await self._reject(send, scope)

    async def _reject(self, send: Send, scope: Scope) -> None:
        state = scope.get("state") or {}
        request_id = state.get("request_id") if isinstance(state, dict) else None
        body = json.dumps(
            {
                "type": "about:blank",
                "title": "Payload Too Large",
                "status": 413,
                "code": "payload_too_large",
                "detail": f"request body exceeds {self.max_bytes} bytes",
                "request_id": request_id if isinstance(request_id, str) else None,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", PROBLEM_MEDIA_TYPE.encode()),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
