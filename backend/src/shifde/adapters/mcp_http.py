"""`mcp_http` connection adapter: Model Context Protocol servers over streamable HTTP.

Built on the official `mcp` Python SDK. stdio transport is deliberately NOT supported (it would
spawn local processes). All traffic goes through the EgressGuard (pinned connect, size caps).
Tools that are not annotated read-only need `write` permission to be called.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar  # Any: MCP JSON payloads

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from shifde.adapters.base import (
    AdapterDescriptor,
    AdapterFailure,
    Capability,
    CheckRecorder,
    ConfigError,
    ConnectionConfig,
    DiscoveryResult,
    ErrorClass,
    ExecContext,
    InvokeResult,
    ProbeOutcome,
    validate_against_schema,
)
from shifde.adapters.httputil import auth_headers, classify_exception, standard_network_checks
from shifde.core.egress import EgressViolation

MAX_RESULT_CHARS = 256_000


def run_sync[T](factory: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine to completion from synchronous code (also if a loop is already running in
    this thread, in which case it runs on a helper thread)."""

    async def runner() -> T:
        return await factory()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(runner())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, runner()).result()


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for inner in exc.exceptions:
            leaves.extend(_leaf_exceptions(inner))
        return leaves
    return [exc]


def classify_mcp(exc: BaseException) -> AdapterFailure:
    leaves = _leaf_exceptions(exc)
    for leaf in leaves:  # the most specific signal wins
        if isinstance(leaf, AdapterFailure):
            return leaf
    for leaf in leaves:
        if isinstance(leaf, EgressViolation):
            return classify_exception(leaf)
    for leaf in leaves:
        if isinstance(leaf, httpx.HTTPStatusError):
            code = leaf.response.status_code
            if code in (401, 403):
                return AdapterFailure(
                    ErrorClass.AUTH, f"the MCP server rejected the credentials (HTTP {code})"
                )
            return AdapterFailure(ErrorClass.UPSTREAM, f"the MCP server answered HTTP {code}")
    for leaf in leaves:
        if isinstance(leaf, TimeoutError | httpx.TimeoutException):
            return AdapterFailure(ErrorClass.TIMEOUT, "the MCP request timed out")
    for leaf in leaves:
        if isinstance(leaf, httpx.HTTPError):
            return classify_exception(leaf)
    return AdapterFailure(ErrorClass.UPSTREAM, f"MCP error: {type(leaves[0]).__name__}")


async def _call[T](
    ctx: ExecContext,
    conn: ConnectionConfig,
    fn: Callable[[ClientSession], Awaitable[T]],
) -> T:
    timeout = ctx.egress.timeout_seconds
    headers = auth_headers(conn)
    async with (
        asyncio.timeout(timeout * 3),
        streamablehttp_client(
            str(conn.config["url"]),
            headers=headers or None,
            timeout=timeout,
            sse_read_timeout=timeout * 3,
            httpx_client_factory=ctx.egress.mcp_client_factory(),
        ) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await fn(session)


def _tool_dict(tool: Any) -> dict[str, Any]:  # Any: SDK tool model
    annotations = getattr(tool, "annotations", None)
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.inputSchema,
        "read_only": bool(annotations and getattr(annotations, "readOnlyHint", None) is True),
    }


class McpSession:
    def __init__(self, ctx: ExecContext, conn: ConnectionConfig) -> None:
        self._ctx = ctx
        self._conn = conn

    def invoke(self, request: dict[str, Any], *, write: bool = False) -> InvokeResult:
        tool_name = str(request.get("tool", ""))
        arguments = request.get("arguments") or {}
        if not tool_name or not isinstance(arguments, dict):
            raise AdapterFailure(ErrorClass.CONFIG, "request needs `tool` and object `arguments`")
        allow = self._conn.config.get("tool_allowlist")
        if allow is not None and tool_name not in allow:
            raise AdapterFailure(ErrorClass.CAPABILITY, f"tool {tool_name!r} is not allowed")

        async def go(session: ClientSession) -> InvokeResult:
            listed = await session.list_tools()
            tools = {t.name: _tool_dict(t) for t in listed.tools}
            if tool_name not in tools:
                raise AdapterFailure(ErrorClass.CAPABILITY, f"unknown tool {tool_name!r}")
            if not tools[tool_name]["read_only"] and not write:
                raise AdapterFailure(
                    ErrorClass.CAPABILITY,
                    "this tool is not annotated read-only; it needs read_write access plus "
                    "connection:write",
                )
            result = await session.call_tool(tool_name, arguments)
            content: list[dict[str, Any]] = []
            size = 0
            truncated = False
            for part in result.content:
                text = getattr(part, "text", None)
                item = (
                    {"type": part.type, "text": text} if text is not None else {"type": part.type}
                )
                size += len(text or "")
                if size > MAX_RESULT_CHARS:
                    truncated = True
                    break
                content.append(item)
            return InvokeResult(
                {
                    "is_error": bool(result.isError),
                    "content": content,
                    "structured": getattr(result, "structuredContent", None),
                },
                truncated,
            )

        try:
            return run_sync(lambda: _call(self._ctx, self._conn, go))
        except BaseException as exc:  # noqa: BLE001 - includes ExceptionGroup; classified
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            raise classify_mcp(exc) from exc

    def close(self) -> None:
        return None


class McpHttpAdapter:
    descriptor: ClassVar[AdapterDescriptor] = AdapterDescriptor(
        key="mcp_http",
        kind="connection",
        version="1.0.0",
        display_name="MCP server (streamable HTTP)",
        capabilities=frozenset({Capability.MCP_TOOLS}),
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "format": "uri", "title": "MCP endpoint URL"},
                "tool_allowlist": {
                    "type": "array",
                    "items": {"type": "string"},
                    "title": "Callable tools (omit = all discovered tools; [] = none)",
                },
                "api_key_header": {"type": "string", "default": "X-API-Key"},
            },
        },
        secret_fields=("token", "api_key"),
        destination_fields=("url",),
        auth_methods=("none", "bearer", "api_key"),
        connection_type="mcp",
    )

    def validate_config(self, config: dict[str, Any]) -> list[ConfigError]:
        def extra(cfg: dict[str, Any]) -> list[ConfigError]:
            url = str(cfg.get("url", ""))
            if not url.startswith(("http://", "https://")):
                return [ConfigError("url", "must be an http(s) URL (stdio is not supported)")]
            return []

        return validate_against_schema(self.descriptor, config, extra=extra)

    def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome:
        rec = CheckRecorder()
        errors = self.validate_config(conn.config)
        if errors:
            rec.fail("config", "; ".join(f"{e.path}: {e.message}" for e in errors)[:400])
            return ProbeOutcome(rec.finish(), ErrorClass.CONFIG, "configuration is invalid")
        rec.ok("config", "configuration is valid")
        url = str(conn.config["url"])
        if not standard_network_checks(ctx, url, rec):
            return ProbeOutcome(rec.finish(), ErrorClass.EGRESS, "destination denied by policy")

        async def go(session: ClientSession) -> int:
            return len((await session.list_tools()).tools)

        try:
            count = run_sync(lambda: _call(ctx, conn, go))
        except BaseException as exc:  # noqa: BLE001 - classified
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            failure = classify_mcp(exc)
            if failure.error_class is ErrorClass.AUTH:
                rec.ok("tls", "transport established") if url.startswith("https") else rec.skip(
                    "tls", "plain http"
                )
                rec.fail("auth", failure.message)
            elif failure.error_class is ErrorClass.TLS:
                rec.fail("tls", failure.message)
            else:
                rec.fail("capability", failure.message)
            return ProbeOutcome(rec.finish(), failure.error_class, failure.message)
        if url.startswith("https://"):
            rec.ok("tls", "certificate verified for the requested host name")
        else:
            rec.skip("tls", "plain http")
        rec.ok("auth", "credentials accepted" if conn.credential else "no credentials configured")
        rec.ok("capability", f"MCP initialize and tools/list succeeded ({count} tool(s))")
        return ProbeOutcome(rec.finish())

    def discover(self, ctx: ExecContext, conn: ConnectionConfig) -> DiscoveryResult:
        async def go(session: ClientSession) -> list[dict[str, Any]]:
            return [_tool_dict(t) for t in (await session.list_tools()).tools]

        try:
            tools = run_sync(lambda: _call(ctx, conn, go))
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            raise classify_mcp(exc) from exc
        return DiscoveryResult(
            "mcp_tools",
            {
                "tools": [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["input_schema"],
                        "read_only": t["read_only"],
                    }
                    for t in tools
                ]
            },
        )

    def open(self, ctx: ExecContext, conn: ConnectionConfig) -> McpSession:
        return McpSession(ctx, conn)
