"""Built-in conformance suites (one per implemented adapter) and their registration.

Targets: REST -> local HTTP server; MCP -> local server built with the official `mcp` SDK over
streamable HTTP; PostgreSQL -> the real `shifde_sample` database; OpenAI-compatible -> local fake
server; fake -> itself (deterministic). Anthropic has only a *live smoke* (needs ANTHROPIC_API_KEY;
skipped, and therefore never recorded, without it).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from functools import partial
from typing import Any

from sqlalchemy.engine import make_url

from shifde.adapters.base import (
    AdapterFailure,
    Capability,
    ChatMessage,
    ChatRequest,
    ErrorClass,
    ExecContext,
    Filter,
    SourceQueryPlan,
)
from shifde.adapters.conformance import (
    Check,
    ConformanceSuite,
    SuiteSkipped,
    connection,
    exec_ctx,
    expect,
    loopback_guard,
)
from shifde.adapters.mcp_http import McpHttpAdapter
from shifde.adapters.model_providers import (
    AnthropicProvider,
    FakeProvider,
    OpenAICompatibleProvider,
)
from shifde.adapters.postgres import PostgresAdapter
from shifde.adapters.rest_http import RestHttpAdapter
from shifde.adapters.testservers import McpTestServer, OpenAIFakeServer, RestTestServer
from shifde.core.egress import EgressGuard, EgressPolicy

C = Capability


def _raises(fn: Callable[[], object], error_class: ErrorClass) -> str:
    try:
        fn()
    except AdapterFailure as exc:
        expect(exc.error_class is error_class, f"expected {error_class}, got {exc.error_class}")
        return f"failed as expected ({exc.error_class})"
    raise AssertionError(f"expected an AdapterFailure({error_class}) but the call succeeded")


# =============================================================================== REST
class RestHttpSuite(ConformanceSuite):
    adapter_key = "rest.http"

    def target(self) -> AbstractContextManager[Any]:
        return RestTestServer(token="rest-suite-token")  # scan-secrets: allow, local test server

    def checks(self) -> Sequence[Check]:
        adapter = RestHttpAdapter()

        def conn(t: RestTestServer, **kw: Any) -> Any:
            cfg = {"base_url": t.base_url, "health_path": "/health"}
            return connection("rest.http", cfg, **kw)

        def ctx(**policy: Any) -> Any:
            return exec_ctx(loopback_guard(**policy))

        def test_passes(t: RestTestServer) -> str:
            out = adapter.test(ctx(), conn(t, auth_method="bearer", secret=t.token))
            expect(
                out.passed,
                f"connection test failed: {[c for c in out.checks if c.status == 'fail']}",
            )
            expect(
                [c.name for c in out.checks]
                == ["config", "egress", "dns", "tls", "auth", "capability"],
                "check names",
            )
            return "all six checks reported"

        def auth_failure(t: RestTestServer) -> str:
            out = adapter.test(ctx(), conn(t, auth_method="bearer", secret="wrong"))
            expect(
                not out.passed and out.error_class is ErrorClass.AUTH,
                "wrong token must classify as auth",
            )
            return "401 classified as auth"

        def invoke_get(t: RestTestServer) -> str:
            s = adapter.open(ctx(), conn(t, auth_method="bearer", secret=t.token))
            r = s.invoke({"method": "GET", "path": "/echo", "query": {"a": "1"}})
            expect(
                r.result["status"] == 200 and r.result["body"]["query"] == {"a": "1"}, "echo body"
            )
            return "GET with query"

        def writes_gated(t: RestTestServer) -> str:
            s = adapter.open(ctx(), conn(t, auth_method="bearer", secret=t.token))
            _raises(
                lambda: s.invoke({"method": "POST", "path": "/echo", "body": {}}),
                ErrorClass.CAPABILITY,
            )
            r = s.invoke({"method": "POST", "path": "/echo", "body": {"x": 1}}, write=True)
            expect(r.result["body"]["body"] == {"x": 1}, "POST body echoed")
            return "POST refused without write, accepted with write"

        def path_escape(t: RestTestServer) -> str:
            s = adapter.open(ctx(), conn(t))
            for bad in ("http://evil.test/x", "//evil.test/x", "echo", "/a\\b"):
                _raises(partial(s.invoke, {"method": "GET", "path": bad}), ErrorClass.CONFIG)
            return "absolute / scheme-relative paths refused"

        def redirects(t: RestTestServer) -> str:
            s = adapter.open(ctx(), conn(t, auth_method="bearer", secret=t.token))
            r = s.invoke({"method": "GET", "path": "/redirect"})
            expect(r.result["status"] == 302, "redirects are not followed by default")
            s2 = adapter.open(
                ctx(allow_redirects=True), conn(t, auth_method="bearer", secret=t.token)
            )
            _raises(lambda: s2.invoke({"method": "GET", "path": "/redirect"}), ErrorClass.EGRESS)
            return "redirect not followed; redirect to a metadata address denied when enabled"

        def oversize(t: RestTestServer) -> str:
            s = adapter.open(ctx(max_response_bytes=100_000), conn(t))
            _raises(lambda: s.invoke({"method": "GET", "path": "/big"}), ErrorClass.UPSTREAM)
            return "response above the size cap rejected"

        def private_denied(t: RestTestServer) -> str:
            guard = EgressGuard([EgressPolicy(allowed_schemes=frozenset({"http", "https"}))])
            out = adapter.test(exec_ctx(guard), conn(t))
            expect(
                not out.passed and out.error_class is ErrorClass.EGRESS, "loopback must be denied"
            )
            return "loopback target denied by the default policy"

        def discover(t: RestTestServer) -> str:
            d = adapter.discover(ctx(), conn(t, auth_method="bearer", secret=t.token))
            expect(d.kind == "openapi", "kind")
            paths = {(p["method"], p["path"]) for p in d.payload["paths"]}
            expect(("GET", "/echo") in paths and ("POST", "/echo") in paths, "paths discovered")
            return f"{len(paths)} operation(s)"

        def config_validation(_t: RestTestServer) -> str:
            errs = adapter.validate_config({"base_url": "http://x", "token": "abc"})
            expect(errs, "credentials in config must be rejected")
            expect(
                not adapter.validate_config({"base_url": "https://api.example.com"}), "valid config"
            )
            return "secret-like config keys rejected"

        return [
            Check("config_validation", (C.REST_CALL,), config_validation),
            Check("connection_test_passes", (C.REST_CALL,), test_passes),
            Check("auth_failure_is_classified", (C.REST_CALL,), auth_failure),
            Check("invoke_get", (C.REST_CALL,), invoke_get),
            Check("writes_are_gated", (C.REST_CALL,), writes_gated),
            Check("path_cannot_leave_base_url", (C.REST_CALL,), path_escape),
            Check("redirects_not_followed_and_revalidated", (C.REST_CALL,), redirects),
            Check("oversized_response_rejected", (C.REST_CALL,), oversize),
            Check("private_destination_denied", (C.REST_CALL,), private_denied),
            Check("openapi_discovery", (C.DISCOVER_OPENAPI,), discover),
        ]


# =============================================================================== MCP
class McpHttpSuite(ConformanceSuite):
    adapter_key = "mcp_http"

    def target(self) -> AbstractContextManager[Any]:
        return McpTestServer(token="mcp-suite-token")  # scan-secrets: allow, local test server

    def checks(self) -> Sequence[Check]:
        adapter = McpHttpAdapter()

        def conn(t: McpTestServer, **kw: Any) -> Any:
            return connection("mcp_http", {"url": t.url}, **kw)

        def ctx() -> Any:
            return exec_ctx(loopback_guard(timeout_seconds=15.0))

        def test_passes(t: McpTestServer) -> str:
            out = adapter.test(ctx(), conn(t, auth_method="bearer", secret=t.token or ""))
            expect(
                out.passed,
                f"MCP connection test failed: {[c for c in out.checks if c.status == 'fail']}",
            )
            return "initialize + tools/list over streamable HTTP"

        def auth_failure(t: McpTestServer) -> str:
            out = adapter.test(ctx(), conn(t, auth_method="bearer", secret="wrong"))
            expect(not out.passed and out.error_class is ErrorClass.AUTH, f"got {out.error_class}")
            return "401 classified as auth"

        def discover(t: McpTestServer) -> str:
            d = adapter.discover(ctx(), conn(t, auth_method="bearer", secret=t.token or ""))
            names = {x["name"] for x in d.payload["tools"]}
            expect({"add", "echo", "wipe"} <= names, f"tools: {names}")
            add = next(x for x in d.payload["tools"] if x["name"] == "add")
            expect("properties" in add["input_schema"], "input schema reported")
            return f"{len(names)} tool(s)"

        def call_read_only(t: McpTestServer) -> str:
            s = adapter.open(ctx(), conn(t, auth_method="bearer", secret=t.token or ""))
            r = s.invoke({"tool": "add", "arguments": {"a": 2, "b": 3}})
            expect(not r.result["is_error"], "tool error")
            expect("5" in str(r.result["content"]) or r.result["structured"], "add result")
            return "read-only tool call"

        def side_effects_gated(t: McpTestServer) -> str:
            s = adapter.open(ctx(), conn(t, auth_method="bearer", secret=t.token or ""))
            _raises(
                lambda: s.invoke({"tool": "wipe", "arguments": {"target": "x"}}),
                ErrorClass.CAPABILITY,
            )
            expect("wipe" not in t.calls, "the gated tool must not have been executed")
            r = s.invoke({"tool": "wipe", "arguments": {"target": "x"}}, write=True)
            expect(not r.result["is_error"] and "wipe" in t.calls, "executed with write")
            return "non-read-only tool needs write"

        def unknown_tool(t: McpTestServer) -> str:
            s = adapter.open(ctx(), conn(t, auth_method="bearer", secret=t.token or ""))
            return _raises(
                lambda: s.invoke({"tool": "nope", "arguments": {}}), ErrorClass.CAPABILITY
            )

        def egress_denied(t: McpTestServer) -> str:
            guard = EgressGuard([EgressPolicy(allowed_schemes=frozenset({"http"}))])
            out = adapter.test(exec_ctx(guard), conn(t))
            expect(not out.passed and out.error_class is ErrorClass.EGRESS, "loopback denied")
            return "loopback target denied by the default policy"

        def stdio_rejected(_t: McpTestServer) -> str:
            errs = adapter.validate_config({"url": "stdio://npx some-server"})
            expect(errs, "non-http MCP endpoints must be rejected")
            return "stdio endpoints rejected"

        return [
            Check("connection_test_passes", (C.MCP_TOOLS,), test_passes),
            Check("auth_failure_is_classified", (C.MCP_TOOLS,), auth_failure),
            Check("tool_discovery", (C.MCP_TOOLS,), discover),
            Check("read_only_tool_call", (C.MCP_TOOLS,), call_read_only),
            Check("side_effect_tools_are_gated", (C.MCP_TOOLS,), side_effects_gated),
            Check("unknown_tool_refused", (C.MCP_TOOLS,), unknown_tool),
            Check("private_destination_denied", (C.MCP_TOOLS,), egress_denied),
            Check("stdio_not_supported", (C.MCP_TOOLS,), stdio_rejected),
        ]


# =============================================================================== PostgreSQL
def sample_database_url() -> str | None:
    """`SHIFDE_SAMPLE_DATABASE_URL` from the environment or the dev env file (development)."""
    from shifde.core.devenv import read_dev_env

    if os.environ.get("SHIFDE_SAMPLE_DATABASE_URL"):
        return os.environ["SHIFDE_SAMPLE_DATABASE_URL"]
    if os.environ.get("SHIFDE_ENV", "").lower() == "development":
        return read_dev_env().get("SHIFDE_SAMPLE_DATABASE_URL")
    return None


class PostgresSuite(ConformanceSuite):
    adapter_key = "postgres"

    def __init__(self, url: str | None = None) -> None:
        self._url = url

    @contextmanager
    def _target(self) -> Iterator[dict[str, Any]]:
        url = self._url or sample_database_url()
        if not url:
            raise SuiteSkipped("no sample database: run `shifde-admin dev-sample-data` first")
        u = make_url(url)
        yield {
            "host": u.host or "127.0.0.1",
            "port": u.port or 5432,
            "database": u.database,
            "user": u.username,
            "password": u.password or "",
        }

    def target(self) -> AbstractContextManager[Any]:
        return self._target()

    def checks(self) -> Sequence[Check]:
        adapter = PostgresAdapter()

        def conn(t: dict[str, Any], password: str | None = None, **extra: Any) -> Any:
            cfg = {
                "host": t["host"],
                "port": t["port"],
                "database": t["database"],
                "user": t["user"],
                "sslmode": "prefer",
                **extra,
            }
            return connection(
                "postgres",
                cfg,
                auth_method="basic",
                secret=t["password"] if password is None else password,
            )

        def ctx() -> ExecContext:
            return exec_ctx(loopback_guard())

        def test_passes(t: dict[str, Any]) -> str:
            out = adapter.test(ctx(), conn(t))
            expect(out.passed, f"{[c for c in out.checks if c.status == 'fail']}")
            return "config/egress/dns/tls/auth/capability all reported"

        def auth_failure(t: dict[str, Any]) -> str:
            out = adapter.test(ctx(), conn(t, password="definitely-wrong"))
            expect(not out.passed and out.error_class is ErrorClass.AUTH, f"got {out.error_class}")
            return "bad password classified as auth"

        def discover(t: dict[str, Any]) -> str:
            d = adapter.discover(ctx(), conn(t))
            tables = {x["name"]: x for x in d.payload["tables"]}
            expect(
                {"customers", "accounts", "service_requests", "employees"} <= set(tables),
                str(set(tables)),
            )
            cols = {c["name"]: c for c in tables["customers"]["columns"]}
            expect(
                "tax_id" in cols and "type" in cols["tax_id"] and "nullable" in cols["tax_id"],
                "columns",
            )
            return f"{len(tables)} table(s)"

        def read_query(t: dict[str, Any]) -> str:
            s = adapter.open(ctx(), conn(t))
            r = s.invoke(
                {
                    "sql": (
                        "SELECT region, count(*) AS n FROM customers "
                        "WHERE region = %s GROUP BY region"
                    ),
                    "params": ["north"],
                }
            )
            expect(r.result["columns"] == ["region", "n"] and r.result["rows"], "grouped rows")
            return "parameterized read"

        def compile_execute(t: dict[str, Any]) -> str:
            plan = SourceQueryPlan(
                table="customers",
                schema="public",
                columns=["id", "name"],
                filters=[
                    Filter("region", "=", "north"),
                    Filter("name", "like", "%'; DROP TABLE x;--"),
                ],
                order_by=[("id", "asc")],
                limit=5,
            )
            q = adapter.compile(plan)
            expect('"public"."customers"' in q.sql and '"id", "name"' in q.sql, q.sql)
            expect(
                "DROP" not in q.sql and q.params[1] == "%'; DROP TABLE x;--",
                "values are parameters",
            )
            weird = adapter.compile(SourceQueryPlan(table='we"ird', columns=['a"b']))
            expect('"we""ird"' in weird.sql and '"a""b"' in weird.sql, weird.sql)
            res = adapter.execute(ctx(), conn(t), q, max_rows=10)
            expect(res.result["row_count"] == 0, "the injection-looking LIKE value matches nothing")
            return "identifiers quoted, values bound"

        def read_only(t: dict[str, Any]) -> str:
            s = adapter.open(ctx(), conn(t))
            _raises(lambda: s.invoke({"sql": "DELETE FROM customers"}), ErrorClass.CAPABILITY)
            _raises(
                lambda: s.invoke(
                    {"sql": "WITH d AS (DELETE FROM customers RETURNING id) SELECT * FROM d"}
                ),
                ErrorClass.CAPABILITY,
            )
            _raises(lambda: s.invoke({"sql": "SELECT 1; SELECT 2"}), ErrorClass.CONFIG)
            return "writes refused in the READ ONLY transaction; multiple statements refused"

        def least_privilege(t: dict[str, Any]) -> str:
            s = adapter.open(ctx(), conn(t))
            _raises(
                lambda: s.invoke({"sql": "INSERT INTO customers (name) VALUES ('x')"}, write=True),
                ErrorClass.CAPABILITY,
            )
            return "the reader role cannot write even when the adapter allows writes"

        def row_cap(t: dict[str, Any]) -> str:
            s = adapter.open(ctx(), conn(t, max_rows=2))
            r = s.invoke({"sql": "SELECT * FROM customers ORDER BY id"})
            expect(r.truncated and len(r.result["rows"]) == 2, "row cap")
            return "truncated at the row cap"

        def timeout(t: dict[str, Any]) -> str:
            s = adapter.open(ctx(), conn(t, statement_timeout_ms=200))
            return _raises(lambda: s.invoke({"sql": "SELECT pg_sleep(5)"}), ErrorClass.TIMEOUT)

        def private_denied(t: dict[str, Any]) -> str:
            guard = EgressGuard([EgressPolicy(allowed_schemes=frozenset({"postgresql"}))])
            out = adapter.test(exec_ctx(guard), conn(t))
            expect(not out.passed and out.error_class is ErrorClass.EGRESS, "loopback denied")
            return "loopback database denied by the default policy"

        return [
            Check("connection_test_passes", (C.QUERY_SQL,), test_passes),
            Check("auth_failure_is_classified", (C.QUERY_SQL,), auth_failure),
            Check("parameterized_read", (C.QUERY_SQL,), read_query),
            Check("compile_quotes_identifiers_and_binds_values", (C.QUERY_SQL,), compile_execute),
            Check("read_only_transaction", (C.QUERY_SQL,), read_only),
            Check("reader_role_is_least_privilege", (C.QUERY_SQL,), least_privilege),
            Check("row_cap", (C.QUERY_SQL,), row_cap),
            Check("statement_timeout", (C.QUERY_SQL,), timeout),
            Check("private_destination_denied", (C.QUERY_SQL,), private_denied),
            Check("schema_discovery", (C.DISCOVER_SCHEMA,), discover),
        ]


# =============================================================================== model providers
class FakeProviderSuite(ConformanceSuite):
    adapter_key = "fake"

    def target(self) -> AbstractContextManager[Any]:
        return _null_target()

    def checks(self) -> Sequence[Check]:
        p = FakeProvider()
        ctx = exec_ctx(loopback_guard())
        cfg = {"script": [{"match": "ping", "reply": "pong"}], "models": ["fake-chat", "fake-big"]}
        conn = connection("fake", cfg)

        def req(text: str) -> ChatRequest:
            return ChatRequest([ChatMessage("user", text)])

        def scripted(_t: Any) -> str:
            a = p.generate(ctx, conn, "fake-chat", req("ping please"))
            b = p.generate(ctx, conn, "fake-chat", req("ping please"))
            expect(a.output == "pong" and a == b, "deterministic scripted reply")
            c = p.generate(ctx, conn, "fake-chat", req("hello"))
            expect(c.output == "fake: hello" and c.usage.input_tokens == 1, "default echo reply")
            return "deterministic"

        def errors(_t: Any) -> str:
            _raises(lambda: p.generate(ctx, conn, "m", req("__fail__")), ErrorClass.UPSTREAM)
            _raises(lambda: p.generate(ctx, conn, "m", req("__ratelimit__")), ErrorClass.RATE_LIMIT)
            return "failure modes are classified"

        def listing(_t: Any) -> str:
            expect(
                [m.model_key for m in p.list_models(ctx, conn)] == ["fake-chat", "fake-big"],
                "models",
            )
            return "scripted model list"

        def health(_t: Any) -> str:
            expect(p.health(ctx, conn).status == "healthy", "healthy")
            expect(p.test(ctx, conn).passed, "connection test passes")
            return "healthy"

        return [
            Check("scripted_chat", (C.MODEL_CHAT,), scripted),
            Check("failure_modes", (C.MODEL_CHAT,), errors),
            Check("list_models", (C.MODEL_LIST,), listing),
            Check("health", (C.MODEL_HEALTH,), health),
        ]


@contextmanager
def _null_target() -> Iterator[None]:
    yield None


class OpenAICompatibleSuite(ConformanceSuite):
    adapter_key = "openai_compatible"

    def target(self) -> AbstractContextManager[Any]:
        return OpenAIFakeServer(api_key="oai-suite-key")  # scan-secrets: allow, local test server

    def checks(self) -> Sequence[Check]:
        p = OpenAICompatibleProvider()
        ctx = exec_ctx(loopback_guard())

        def conn(t: OpenAIFakeServer, key: str | None = None) -> Any:
            return connection(
                "openai_compatible",
                {"base_url": f"{t.base_url}/v1"},
                auth_method="bearer",
                secret=key or t.api_key,
            )

        def chat(t: OpenAIFakeServer) -> str:
            r = p.generate(
                ctx,
                conn(t),
                "fake-gpt",
                ChatRequest([ChatMessage("user", "hi there")], max_tokens=16),
            )
            expect(
                r.output == "echo: hi there"
                and r.usage.output_tokens is not None
                and r.usage.output_tokens > 0,
                "chat output + usage",
            )
            expect(r.provider_request_id == "req-test-1", "provider request id")
            expect(t.bodies[-1]["max_tokens"] == 16, "max_tokens forwarded")
            return "chat completion"

        def listing(t: OpenAIFakeServer) -> str:
            ids = [m.model_key for m in p.list_models(ctx, conn(t))]
            expect(ids == ["fake-gpt", "fake-embed"], str(ids))
            return "model list"

        def classified(t: OpenAIFakeServer) -> str:
            _raises(
                lambda: p.generate(
                    ctx, conn(t, "bad"), "fake-gpt", ChatRequest([ChatMessage("user", "x")])
                ),
                ErrorClass.AUTH,
            )
            _raises(
                lambda: p.generate(
                    ctx, conn(t), "error-model", ChatRequest([ChatMessage("user", "x")])
                ),
                ErrorClass.UPSTREAM,
            )
            _raises(
                lambda: p.generate(
                    ctx, conn(t), "ratelimit-model", ChatRequest([ChatMessage("user", "x")])
                ),
                ErrorClass.RATE_LIMIT,
            )
            return "401/500/429 classified"

        def health(t: OpenAIFakeServer) -> str:
            expect(p.health(ctx, conn(t)).status == "healthy", "healthy")
            expect(p.test(ctx, conn(t)).passed, "connection test passes")
            bad = p.health(ctx, conn(t, "bad"))
            expect(bad.status == "unavailable", "bad credentials -> unavailable")
            return "health + connection test"

        return [
            Check("chat_completion", (C.MODEL_CHAT,), chat),
            Check("error_classification", (C.MODEL_CHAT,), classified),
            Check("list_models", (C.MODEL_LIST,), listing),
            Check("health", (C.MODEL_HEALTH,), health),
        ]


class AnthropicLiveSmoke(ConformanceSuite):
    """Live smoke against the real API; skipped (no evidence) unless ANTHROPIC_API_KEY is set."""

    adapter_key = "anthropic"
    suite_version = "live-1"

    @contextmanager
    def _target(self) -> Iterator[str]:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise SuiteSkipped("ANTHROPIC_API_KEY is not set")
        yield key

    def target(self) -> AbstractContextManager[Any]:
        return self._target()

    def checks(self) -> Sequence[Check]:
        p = AnthropicProvider()
        guard = EgressGuard([EgressPolicy(allowed_ports=frozenset({443}))])
        ctx = exec_ctx(guard)

        def conn(key: str) -> Any:
            return connection("anthropic", {}, auth_method="api_key", secret=key)

        def listing(key: str) -> str:
            models = p.list_models(ctx, conn(key))
            expect(models, "at least one model")
            return f"{len(models)} model(s)"

        def chat(key: str) -> str:
            first = p.list_models(ctx, conn(key))[0].model_key
            r = p.generate(
                ctx,
                conn(key),
                first,
                ChatRequest([ChatMessage("user", "Reply with OK")], max_tokens=16),
            )
            expect(r.output, "non-empty output")
            return "live chat"

        def health(key: str) -> str:
            expect(p.health(ctx, conn(key)).status == "healthy", "healthy")
            return "live health"

        return [
            Check("live_list_models", (C.MODEL_LIST,), listing),
            Check("live_chat", (C.MODEL_CHAT,), chat),
            Check("live_health", (C.MODEL_HEALTH,), health),
        ]


SUITES: dict[str, Callable[[], ConformanceSuite]] = {
    "rest.http": RestHttpSuite,
    "mcp_http": McpHttpSuite,
    "postgres": PostgresSuite,
    "fake": FakeProviderSuite,
    "openai_compatible": OpenAICompatibleSuite,
    "anthropic": AnthropicLiveSmoke,
}
