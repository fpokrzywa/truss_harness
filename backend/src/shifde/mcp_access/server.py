"""The `/mcp` endpoint (design 3.1 to 3.4, 10 items 1 to 6 and 15 to 21).

`McpEndpoint` builds a stateless, JSON-response `FastMCP` server whose session manager the API
app's lifespan runs (`run()`), and the ASGI app `create_app` mounts at `/mcp`. In front of the
SDK transport, `McpGuard` checks, in this order:

1. any `Origin` header: 403 `origin_not_allowed` (MCP clients are not browsers);
2. the body cap, twice: a `Content-Length` above 256 KiB is 413 before the body is read, and a
   streamed body is counted and cut at 256 KiB (also 413);
3. the credential: `Authorization: Bearer shf_...` only (`authenticate_bearer_only`), anything
   else one generic 401 `invalid_credentials`;
4. `mcp:access` at the organization's ref (the key mask and the principal's own organization-level
   grant; the key's resource scope narrows the tools, not this check), else 403 `forbidden`, and
   the organization's `enabled` switch, else 403 `mcp_disabled`, each audited in the key's own
   client as `mcp.access_refused`.

A `Host` outside `SHIFDE_MCP_ALLOWED_HOSTS` then gets the SDK's 421. `Gate` runs each
`tools/call`: the per-client (4x) and per-key rate limits, `allowed_tools`, the argument model,
then the handler on a worker thread with a session of its own. Every call writes one
`mcp.tool_called` event, every refusal one `mcp.access_refused`; arguments and results are never
written to an audit row or a log.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Any  # Any: JSON documents, ASGI messages
from uuid import UUID

import anyio
import structlog
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as McpTool
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.pdp import RECORDS_KEY
from shifde.authz.service import ResourceRef
from shifde.core import db
from shifde.core.context import Principal, RequestContext
from shifde.core.deps import get_request_ctx
from shifde.core.errors import AppError, Forbidden, InvalidCredentials, NotFound, RateLimited
from shifde.core.hooks import run_close_hooks
from shifde.core.pagination import encode_cursor
from shifde.core.ratelimit import SlidingWindowLimiter
from shifde.core.settings import Settings
from shifde.identity import service as identity
from shifde.identity.dependencies import authenticate_bearer_only, register_denial_audit
from shifde.mcp_access import store
from shifde.mcp_access.store import SettingsRow
from shifde.mcp_access.tools import TOOLS, Env, Result, Tool, allowed_tools
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

MAX_BYTES = 256 * 1024  # request body cap and tool result size (design 3.1)
WINDOW_SECONDS = 60
CALLER_KEY = "mcp_caller"
NOT_FOUND_TEXT = "not found"  # one text for unknown, another client's and out-of-scope ids
REFUSED = "mcp.access_refused"
CALLED = "mcp.tool_called"
log = structlog.get_logger("shifde.mcp")


@dataclass(frozen=True)
class Caller:
    """The authenticated key, its organization and the organization's MCP settings."""

    principal: Principal
    ctx: RequestContext
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    tenant_status: str
    mcp: SettingsRow
    key_name: str
    key_expires_at: datetime


def _refusal(status: int, code: str, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def _audit(session: Session, caller_ctx: RequestContext, tenant_id: UUID, **event: Any) -> None:
    """One event in the key's own client, committed. `event`: action, outcome, details."""
    db.set_tenant(session, tenant_id)
    principal = caller_ctx.principal
    audit.record(
        session,
        caller_ctx,
        tenant_id=tenant_id,
        target_type="api_key",
        target_id=principal.api_key_id if principal is not None else None,
        **event,
    )
    session.commit()


# ------------------------------------------------------------------ admission (HTTP level)
def _admit(request: Request) -> Caller | Response:
    """Authenticate the key and check `mcp:access` and the organization's switch (sync)."""
    state = request.app.state
    settings: Settings = state.settings
    ctx = get_request_ctx(request)
    session = state.db.session()
    db.init_context(session, ctx.request_id)
    try:
        try:
            auth = authenticate_bearer_only(request, session, settings, ctx)
        except InvalidCredentials:
            return _refusal(401, "invalid_credentials", {"WWW-Authenticate": "Bearer"})
        except RateLimited:
            return _refusal(429, "rate_limited")
        register_denial_audit(session, ctx)
        principal = auth.principal
        tenant_id = principal.tenant_id
        db.set_tenant(session, tenant_id)
        header = tenancy.tenant_header(session, tenant_id) if tenant_id is not None else None
        if tenant_id is None or header is None:  # pragma: no cover - a key always has a client
            return _refusal(401, "invalid_credentials", {"WWW-Authenticate": "Bearer"})
        # organization level: the key mask applies, the key's resource scope does not (the
        # tools apply it to every id), and a workspace or project binding is not enough
        org_level = replace(principal, resource_scope=None)
        try:
            authz.new_pdp(session, org_level, tenant_id).require(
                "mcp:access", ResourceRef(tenant_id=tenant_id)
            )
        except Forbidden:
            session.rollback()
            _audit(
                session,
                ctx,
                tenant_id,
                action=REFUSED,
                outcome="denied",
                details={"reason": "permission"},
            )
            return _refusal(403, "forbidden")
        row = store.read(session, tenant_id)
        if row is None or not row.enabled:
            session.rollback()
            _audit(
                session,
                ctx,
                tenant_id,
                action=REFUSED,
                outcome="denied",
                details={"reason": "disabled"},
            )
            return _refusal(403, "mcp_disabled")
        key_id = principal.api_key_id
        key = identity.get_api_key(session, tenant_id, key_id) if key_id is not None else None
        assert key is not None  # authenticated above
        session.commit()
        return Caller(
            principal=principal,
            ctx=ctx,
            tenant_id=tenant_id,
            tenant_slug=header[0],
            tenant_name=header[1],
            tenant_status=header[2],
            mcp=row,
            key_name=key.name,
            key_expires_at=key.expires_at,
        )
    finally:
        try:
            run_close_hooks(session)
        finally:
            session.close()


class McpGuard:
    """The ASGI middleware in front of the SDK transport (order in the module docstring)."""

    def __init__(self, app: ASGIApp, admit: Callable[[Request], Awaitable[Caller | Response]]):
        self._app = app
        self._admit = admit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - the route is HTTP only
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if "origin" in headers:
            await _refusal(403, "origin_not_allowed")(scope, receive, send)
            return
        length = headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
            await _refusal(413, "payload_too_large")(scope, receive, send)
            return
        messages: deque[Message] = deque()
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > MAX_BYTES:
                await _refusal(413, "payload_too_large")(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        caller = await self._admit(Request(scope))
        if isinstance(caller, Response):
            await caller(scope, receive, send)
            return
        scope.setdefault("state", {})[CALLER_KEY] = caller

        async def replay() -> Message:
            return messages.popleft() if messages else await receive()

        await self._app(scope, replay, send)


# ------------------------------------------------------------------ tool calls
def _text(document: Result) -> str:
    return json.dumps(document, default=str, separators=(",", ":"))


def _size(document: Result) -> int:
    return len(_text(document).encode("utf-8"))


def fit(result: Result) -> Result:
    """`result`, or a truncated form of at most `MAX_BYTES` serialized with `truncated: true`:
    a list result keeps its first items (and a cursor after the last one kept), anything else
    becomes a prefix of its serialized text."""
    if _size(result) <= MAX_BYTES:
        return result
    items = result.get("items")
    if isinstance(items, list):
        kept = list(items)
        while kept and _size({**result, "items": kept, "truncated": True}) > MAX_BYTES:
            kept = kept[: len(kept) // 2]
        last = kept[-1].get("id") if kept and isinstance(kept[-1], dict) else None
        candidate = {**result, "items": kept, "truncated": True}
        if last is not None:
            candidate["next_cursor"] = encode_cursor(UUID(str(last)))
        if _size(candidate) <= MAX_BYTES:
            return candidate
    prefix = _text(result)[: MAX_BYTES // 2]
    while _size({"truncated": True, "partial": prefix}) > MAX_BYTES:
        prefix = prefix[: len(prefix) // 2]
    return {"truncated": True, "partial": prefix}


def _ok(result: Result) -> CallToolResult:
    text = _text(fit(result))
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structuredContent=json.loads(text)
    )


def _error(code: str, message: str, retry_after: int | None = None) -> CallToolResult:
    body: Result = {"error": code, "message": message}
    if retry_after is not None:
        body["retry_after"] = retry_after
    return CallToolResult(content=[TextContent(type="text", text=_text(body))], isError=True)


@dataclass(frozen=True)
class _Outcome:
    result: CallToolResult
    outcome: str  # success | failure | denied
    error_code: str | None
    refused: str | None = None  # the access_refused reason, if any
    run_id: str | None = None


class Gate:
    """Rate limits, `allowed_tools`, argument validation, the handler and the audit."""

    def __init__(self, state: Any) -> None:  # Any: Starlette's app state
        self._state = state
        settings: Settings = state.settings
        self._max_rate = settings.mcp_max_rate_per_minute
        self.key_limiter = SlidingWindowLimiter(
            self._max_rate, message="this API key's MCP rate limit is reached; try again later"
        )
        self.client_limiter = SlidingWindowLimiter(
            4 * self._max_rate,
            message="this organization's MCP rate limit is reached; try again later",
        )

    async def call(self, caller: Caller, name: str, arguments: dict[str, Any]) -> CallToolResult:
        started = time.monotonic()
        tool = TOOLS.get(name)
        label = name if tool is not None else "unknown"  # a client-chosen name is not recorded
        rate = min(caller.mcp.rate_per_minute, self._max_rate)
        try:
            self.client_limiter.check(str(caller.tenant_id), limit=4 * rate)
            self.key_limiter.check(str(caller.principal.api_key_id), limit=rate)
        except RateLimited as exc:
            outcome = _Outcome(
                _error("rate_limited", exc.detail, WINDOW_SECONDS), "denied", "rate_limited", "rate"
            )
            return await self._finish(caller, label, outcome, started)
        if tool is None:
            outcome = _Outcome(_error("unknown_tool", "no such tool"), "failure", "unknown_tool")
            return await self._finish(caller, label, outcome, started)
        if name not in allowed_tools(caller.mcp):
            outcome = _Outcome(
                _error("tool_not_allowed", "this organization does not allow this tool"),
                "denied",
                "tool_not_allowed",
                "tool_not_allowed",
            )
            return await self._finish(caller, label, outcome, started)
        try:
            args = tool.args.model_validate(arguments)
        except ValidationError:  # the message would echo the arguments: none is returned
            outcome = _Outcome(
                _error("invalid_arguments", "the arguments do not match the tool's input schema"),
                "failure",
                "invalid_arguments",
            )
            return await self._finish(caller, label, outcome, started)
        return await anyio.to_thread.run_sync(partial(self._invoke, caller, tool, args, started))

    async def _finish(
        self, caller: Caller, label: str, outcome: _Outcome, started: float
    ) -> CallToolResult:
        await anyio.to_thread.run_sync(partial(self._record, caller, label, outcome, started))
        return outcome.result

    def _session(self, caller: Caller) -> Session:
        session: Session = self._state.db.session()
        db.init_context(session, caller.ctx.request_id)
        db.set_principal(session, caller.principal.principal_id)
        db.set_tenant(session, caller.tenant_id)
        return session

    def _record(self, caller: Caller, label: str, outcome: _Outcome, started: float) -> None:
        session = self._session(caller)
        try:
            self._write(session, caller, label, outcome, started)
        finally:
            session.close()

    @staticmethod
    def _write(
        session: Session, caller: Caller, label: str, outcome: _Outcome, started: float
    ) -> None:
        details: dict[str, Any] = {
            "tool": label,
            "api_key_id": str(caller.principal.api_key_id),
            "outcome": outcome.outcome,
            "error_code": outcome.error_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if outcome.run_id is not None:
            details["run_id"] = outcome.run_id
        _audit(
            session,
            caller.ctx,
            caller.tenant_id,
            action=CALLED,
            outcome=outcome.outcome,
            details=details,
        )
        if outcome.refused is not None:
            _audit(
                session,
                caller.ctx,
                caller.tenant_id,
                action=REFUSED,
                outcome="denied",
                details={"reason": outcome.refused, "tool": label},
            )

    def _invoke(self, caller: Caller, tool: Tool, args: Any, started: float) -> CallToolResult:
        """The handler in a session of its own with the key's client and principal set, as
        `tenancy.dependencies` builds a `TenantScope`; then the audit, then the close hooks."""
        session = self._session(caller)
        try:
            register_denial_audit(session, caller.ctx)
            scope = TenantScope(
                session=session,
                ctx=caller.ctx,
                principal=caller.principal,
                tenant_id=caller.tenant_id,
                tenant_slug=caller.tenant_slug,
                tenant_name=caller.tenant_name,
                tenant_status=caller.tenant_status,
                pdp=authz.new_pdp(session, caller.principal, caller.tenant_id),
            )
            env = Env(
                runtime=self._state.integration,
                hooks=self._state.governance,
                mcp=caller.mcp,
                key_name=caller.key_name,
                key_expires_at=caller.key_expires_at,
            )
            outcome = self._run(session, scope, env, tool, args)
            self._write(session, caller, tool.name, outcome, started)
            return outcome.result
        finally:
            try:
                run_close_hooks(session)
            finally:
                session.close()

    @staticmethod
    def _run(session: Session, scope: TenantScope, env: Env, tool: Tool, args: Any) -> _Outcome:
        try:
            result = tool.handler(scope, env, args)
        except NotFound:
            session.rollback()
            return _Outcome(_error("not_found", NOT_FOUND_TEXT), "failure", "not_found")
        except Forbidden:
            session.rollback()
            denied = [r for r in session.info.get(RECORDS_KEY, []) if r.decision == "deny"]
            reason = "scope" if any(r.reason == "resource_scope" for r in denied) else "permission"
            return _Outcome(
                _error("forbidden", "the key may not use this tool here"),
                "denied",
                "forbidden",
                reason,
            )
        except AppError as exc:
            session.rollback()
            return _Outcome(_error(exc.code, exc.detail), "failure", exc.code)
        except Exception:  # noqa: BLE001 - one tool error, never the arguments in the log
            session.rollback()
            log.error("mcp.tool_failed", tool=tool.name, exc_info=True)
            return _Outcome(
                _error("internal_error", "an unexpected error occurred"),
                "failure",
                "internal_error",
            )
        session.rollback()  # services commit their own writes
        run_id = result.get("run_id") if tool.name == "start_run" else None
        return _Outcome(_ok(result), "success", None, run_id=run_id)


class McpServer(FastMCP):
    """FastMCP with the v1 tool list and every `tools/call` through the `Gate`."""

    def __init__(self, gate: Gate, settings: Settings) -> None:
        super().__init__(
            name="SETH",
            stateless_http=True,
            json_response=True,
            streamable_http_path="/",
            max_request_body_size=MAX_BYTES,
            log_level="WARNING",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=list(settings.mcp_allowed_hosts),
                allowed_origins=[],
            ),
        )
        self._gate = gate
        # the SDK logs a called tool's name when it is not listed: client text, never logged
        logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.ERROR)

    async def list_tools(self) -> list[McpTool]:
        return [
            McpTool(name=t.name, description=t.description, inputSchema=t.args.model_json_schema())
            for t in TOOLS.values()
        ]

    async def call_tool(  # type: ignore[override]  # always a CallToolResult
        self, name: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        request = self.get_context().request_context.request
        caller = getattr(request.state, CALLER_KEY, None) if request is not None else None
        if not isinstance(caller, Caller):  # pragma: no cover - McpGuard admits every request
            return _error("unauthenticated", "no authenticated caller")
        return await self._gate.call(caller, name, arguments)


class McpEndpoint:
    """The server, its session manager (run by the API app's lifespan) and the guarded ASGI app
    `create_app` mounts at `/mcp`."""

    def __init__(self, state: Any) -> None:  # Any: Starlette's app state
        settings: Settings = state.settings
        self.gate = Gate(state)
        self.server = McpServer(self.gate, settings)
        self.server.streamable_http_app()  # creates the session manager
        manager = self.server.session_manager

        async def transport(scope: Scope, receive: Receive, send: Send) -> None:
            await manager.handle_request(scope, receive, send)

        async def admit(request: Request) -> Caller | Response:
            return await anyio.to_thread.run_sync(partial(_admit, request))

        self.app = McpGuard(transport, admit)

    def run(self) -> Any:  # Any: the session manager's async context manager
        return self.server.session_manager.run()
