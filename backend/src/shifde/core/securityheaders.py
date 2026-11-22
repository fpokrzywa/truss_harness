"""Go-live security headers on every API response (SP4 design section 5, B-2772).

`security_headers` is the one list: `SecurityHeadersMiddleware` applies it inside the stack, and
the catch-all 500 handler (`core/problem.py`), which Starlette's `ServerErrorMiddleware` runs
outside every user middleware, applies it itself."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shifde.core.settings import Settings

CSP = "default-src 'none'; frame-ancestors 'none'"
CSP_HEADER = "Content-Security-Policy"


def security_headers(settings: Settings) -> dict[str, str]:
    """The headers of every response; `Cache-Control` is a default a response may set itself."""
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Opener-Policy": "same-origin",
        CSP_HEADER: CSP,
        "Cache-Control": "no-store",
    }
    if settings.env == "production" and settings.hsts_max_age_seconds > 0:
        headers["Strict-Transport-Security"] = (
            f"max-age={settings.hsts_max_age_seconds}; includeSubDomains"
        )
    return headers


def _is_dev_docs(path: str) -> bool:
    """Swagger UI needs scripts and styles: its pages carry no CSP in development."""
    return path == "/api/openapi.json" or path == "/api/docs" or path.startswith("/api/docs/")


class SecurityHeadersMiddleware:
    """Add `security_headers(settings)` to every http response start: an existing
    `Cache-Control` is kept, the other headers replace same-named ones."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        added = security_headers(self.settings)
        if self.settings.env == "development" and _is_dev_docs(scope.get("path", "")):
            del added[CSP_HEADER]
        encoded = {name.lower().encode(): value.encode() for name, value in added.items()}

        async def headers_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = list(message.get("headers", []))
                apply = dict(encoded)
                if any(name.lower() == b"cache-control" for name, _ in existing):
                    del apply[b"cache-control"]  # the route chose its own caching
                headers = [(n, v) for n, v in existing if n.lower() not in apply]
                headers.extend(apply.items())
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, headers_send)
