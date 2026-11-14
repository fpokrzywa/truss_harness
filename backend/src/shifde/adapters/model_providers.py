"""Model provider adapters: `fake` (deterministic), `openai_compatible`, `anthropic`.

Each doubles as the connection adapter of its `model_api` connection: `test` probes the endpoint,
`discover` is not offered (the provider API lists models through `list_models`).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, ClassVar  # Any: provider JSON bodies

import httpx

from shifde.adapters.base import (
    AdapterDescriptor,
    AdapterFailure,
    Capability,
    ChatRequest,
    ChatResponse,
    CheckRecorder,
    ConfigError,
    ConnectionConfig,
    DiscoveryResult,
    ErrorClass,
    ExecContext,
    HealthResult,
    InvokeResult,
    ModelInfo,
    ProbeOutcome,
    Usage,
    validate_against_schema,
)
from shifde.adapters.httputil import auth_headers, classify_exception, standard_network_checks
from shifde.core.egress import EgressViolation

SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _tokens(value: Any) -> int | None:
    """A provider token count, or None when it is missing or not a non-negative integer."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _checked_max_tokens(value: Any) -> int | None:
    """`max_tokens` as the caller gave it: None (not set) or a positive integer. Anything else is
    refused instead of being replaced by a provider default that could overshoot a budget."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AdapterFailure(ErrorClass.CONFIG, "`max_tokens` must be a positive integer")
    return value


class _BaseModelAdapter:
    """Shared plumbing; concrete adapters define `descriptor`, `generate`, `list_models`."""

    descriptor: ClassVar[AdapterDescriptor]

    def validate_config(self, config: dict[str, Any]) -> list[ConfigError]:
        return validate_against_schema(self.descriptor, config, extra=self._extra_checks)

    def _extra_checks(self, config: dict[str, Any]) -> list[ConfigError]:
        return []

    def discover(self, ctx: ExecContext, conn: ConnectionConfig) -> DiscoveryResult:
        raise AdapterFailure(
            ErrorClass.CAPABILITY,
            "model connections have no discovery; list models through the provider",
        )

    def classify_error(self, exc: Exception) -> ErrorClass:
        return classify_exception(exc).error_class

    # connection-style session so `ConnectionService.open_for_use` works for model_api too
    def open(self, ctx: ExecContext, conn: ConnectionConfig) -> _ModelSession:
        return _ModelSession(self, ctx, conn)

    def generate(
        self, ctx: ExecContext, conn: ConnectionConfig, model_key: str, request: ChatRequest
    ) -> ChatResponse:
        raise NotImplementedError

    def list_models(self, ctx: ExecContext, conn: ConnectionConfig) -> list[ModelInfo]:
        raise NotImplementedError

    def health(self, ctx: ExecContext, conn: ConnectionConfig) -> HealthResult:
        start = time.monotonic()
        try:
            models = self.list_models(ctx, conn)
        except AdapterFailure as exc:
            status = "degraded" if exc.error_class is ErrorClass.RATE_LIMIT else "unavailable"
            return HealthResult(status, int((time.monotonic() - start) * 1000), exc.message)  # type: ignore[arg-type]
        return HealthResult(
            "healthy", int((time.monotonic() - start) * 1000), f"{len(models)} model(s) listed"
        )

    def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome:
        rec = CheckRecorder()
        errors = self.validate_config(conn.config)
        if errors:
            rec.fail("config", "; ".join(f"{e.path}: {e.message}" for e in errors)[:400])
            return ProbeOutcome(rec.finish(), ErrorClass.CONFIG, "configuration is invalid")
        rec.ok("config", "configuration is valid")
        base = conn.config.get("base_url")
        if base:
            if not standard_network_checks(ctx, str(base), rec):
                return ProbeOutcome(rec.finish(), ErrorClass.EGRESS, "destination denied by policy")
        else:
            rec.skip("egress", "provider makes no network calls")
            rec.skip("dns", "provider makes no network calls")
        try:
            models = self.list_models(ctx, conn)
        except AdapterFailure as exc:
            if exc.error_class is ErrorClass.TLS:
                rec.fail("tls", exc.message)
            elif exc.error_class is ErrorClass.AUTH:
                rec.ok("tls", "transport established")
                rec.fail("auth", exc.message)
            else:
                rec.fail("capability", exc.message)
            return ProbeOutcome(rec.finish(), exc.error_class, exc.message)
        if str(base or "").startswith("https://"):
            rec.ok("tls", "certificate verified for the requested host name")
        else:
            rec.skip("tls", "plain http or no network")
        rec.ok("auth", "credentials accepted" if conn.credential else "no credentials configured")
        rec.ok("capability", f"model listing succeeded ({len(models)} model(s))")
        return ProbeOutcome(rec.finish())


class _ModelSession:
    def __init__(
        self, adapter: _BaseModelAdapter, ctx: ExecContext, conn: ConnectionConfig
    ) -> None:
        self._adapter = adapter
        self._ctx = ctx
        self._conn = conn

    def invoke(self, request: dict[str, Any], *, write: bool = False) -> InvokeResult:
        from shifde.adapters.base import ChatMessage

        try:
            model = str(request.get("model", ""))
            messages = [
                ChatMessage(role=m["role"], content=str(m["content"]))
                for m in request.get("messages", [])
            ]
        except (AttributeError, TypeError, KeyError) as exc:
            raise AdapterFailure(ErrorClass.CONFIG, "malformed request") from exc
        if not model or not messages:
            raise AdapterFailure(ErrorClass.CONFIG, "request needs `model` and `messages`")
        resp = self._adapter.generate(
            self._ctx,
            self._conn,
            model,
            ChatRequest(messages, _checked_max_tokens(request.get("max_tokens"))),
        )
        return InvokeResult({"output": resp.output, "usage": resp.usage.__dict__})

    def close(self) -> None:
        return None


# =============================================================================== fake
class FakeProvider(_BaseModelAdapter):
    """Deterministic scripted provider (no network). `script` entries `{match, reply}` are tried in
    order against the last user message (substring); otherwise the reply is
    ``"fake: " + <last user message>``. Special user texts: ``__fail__`` -> upstream error,
    ``__ratelimit__`` -> rate-limit error. `latency_ms` adds a fixed delay."""

    descriptor: ClassVar[AdapterDescriptor] = AdapterDescriptor(
        key="fake",
        kind="model_provider",
        version="1.0.0",
        display_name="Fake (deterministic test provider)",
        capabilities=frozenset(
            {Capability.MODEL_CHAT, Capability.MODEL_LIST, Capability.MODEL_HEALTH}
        ),
        config_schema={
            "$schema": SCHEMA,
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "script": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["match", "reply"],
                        "additionalProperties": False,
                        "properties": {"match": {"type": "string"}, "reply": {"type": "string"}},
                    },
                },
                "models": {"type": "array", "items": {"type": "string"}},
                "latency_ms": {"type": "integer", "minimum": 0, "maximum": 30000, "default": 0},
            },
        },
        secret_fields=(),
        auth_methods=("none",),
        connection_type="model_api",
    )

    def generate(
        self, ctx: ExecContext, conn: ConnectionConfig, model_key: str, request: ChatRequest
    ) -> ChatResponse:
        last = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
        if last == "__fail__":
            raise AdapterFailure(ErrorClass.UPSTREAM, "the fake provider was told to fail")
        if last == "__ratelimit__":
            raise AdapterFailure(ErrorClass.RATE_LIMIT, "the fake provider is rate limited")
        delay = int(conn.config.get("latency_ms", 0))
        if delay:
            try:
                ctx.egress.sleep(delay / 1000)  # honours the call deadline
            except EgressViolation as exc:
                raise classify_exception(exc) from exc
        reply = f"fake: {last}"
        for entry in conn.config.get("script", []):
            if entry["match"] in last:
                reply = entry["reply"]
                break
        words = sum(len(m.content.split()) for m in request.messages)
        out = reply
        if request.max_tokens is not None:
            out = " ".join(reply.split()[: max(request.max_tokens, 0)])
        return ChatResponse(
            output=out,
            usage=Usage(input_tokens=words, output_tokens=len(out.split())),
            finish_reason="stop",
            provider_request_id="fake-"
            + hashlib.sha256(f"{model_key}|{last}".encode()).hexdigest()[:12],
        )

    def list_models(self, ctx: ExecContext, conn: ConnectionConfig) -> list[ModelInfo]:
        keys = conn.config.get("models") or ["fake-chat"]
        return [ModelInfo(k, k, 8192, 2048, ("chat",)) for k in keys]


# =============================================================================== openai-compatible
class OpenAICompatibleProvider(_BaseModelAdapter):
    descriptor: ClassVar[AdapterDescriptor] = AdapterDescriptor(
        key="openai_compatible",
        kind="model_provider",
        version="1.0.0",
        display_name="OpenAI-compatible endpoint",
        capabilities=frozenset(
            {Capability.MODEL_CHAT, Capability.MODEL_LIST, Capability.MODEL_HEALTH}
        ),
        config_schema={
            "$schema": SCHEMA,
            "type": "object",
            "additionalProperties": False,
            "required": ["base_url"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "format": "uri",
                    "title": "Base URL (including /v1)",
                },
                "organization": {"type": "string"},
            },
        },
        secret_fields=("api_key", "token"),
        destination_fields=("base_url",),
        auth_methods=("none", "bearer"),
        connection_type="model_api",
    )

    def _extra_checks(self, config: dict[str, Any]) -> list[ConfigError]:
        if not str(config.get("base_url", "")).startswith(("http://", "https://")):
            return [ConfigError("base_url", "must be an absolute http(s) URL")]
        return []

    def _client(self, ctx: ExecContext, conn: ConnectionConfig) -> httpx.Client:
        headers = auth_headers(conn)
        if conn.config.get("organization"):
            headers["OpenAI-Organization"] = str(conn.config["organization"])
        return ctx.egress.client(headers=headers)

    def _request(
        self,
        ctx: ExecContext,
        conn: ConnectionConfig,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = str(conn.config["base_url"]).rstrip("/") + path
        try:
            with self._client(ctx, conn) as client:
                response = client.request(method, url, json=body)
        except Exception as exc:  # noqa: BLE001 - classified; no body/credentials echoed
            raise classify_exception(exc) from exc
        if response.status_code in (401, 403):
            raise AdapterFailure(
                ErrorClass.AUTH,
                f"the endpoint rejected the credentials (HTTP {response.status_code})",
            )
        if response.status_code == 429:
            raise AdapterFailure(ErrorClass.RATE_LIMIT, "the provider rate limited the request")
        if response.status_code >= 400:
            raise AdapterFailure(
                ErrorClass.UPSTREAM, f"the provider answered HTTP {response.status_code}"
            )
        return response

    def generate(
        self, ctx: ExecContext, conn: ConnectionConfig, model_key: str, request: ChatRequest
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": model_key,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        max_tokens = _checked_max_tokens(request.max_tokens)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        response = self._request(ctx, conn, "POST", "/chat/completions", payload)
        try:
            doc = response.json()
            choice = doc["choices"][0]
            usage = doc.get("usage") or {}
            return ChatResponse(
                output=str(choice["message"]["content"] or ""),
                usage=Usage(
                    _tokens(usage.get("prompt_tokens")),
                    _tokens(usage.get("completion_tokens")),
                    _tokens((usage.get("prompt_tokens_details") or {}).get("cached_tokens")),
                ),
                finish_reason=choice.get("finish_reason"),
                provider_request_id=response.headers.get("x-request-id") or doc.get("id"),
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AdapterFailure(
                ErrorClass.UPSTREAM, "the provider returned an unexpected body"
            ) from exc

    def list_models(self, ctx: ExecContext, conn: ConnectionConfig) -> list[ModelInfo]:
        response = self._request(ctx, conn, "GET", "/models")
        try:
            data = response.json()["data"]
            return [ModelInfo(str(m["id"]), str(m["id"])) for m in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterFailure(
                ErrorClass.UPSTREAM, "the provider returned an unexpected body"
            ) from exc


# =============================================================================== anthropic
class AnthropicProvider(OpenAICompatibleProvider):
    """Anthropic Messages API. Experimental: only exercised against fakes unless a live smoke test
    with ANTHROPIC_API_KEY has been run (none is required or recorded by default)."""

    descriptor: ClassVar[AdapterDescriptor] = AdapterDescriptor(
        key="anthropic",
        kind="model_provider",
        version="1.0.0",
        display_name="Anthropic",
        capabilities=frozenset(
            {Capability.MODEL_CHAT, Capability.MODEL_LIST, Capability.MODEL_HEALTH}
        ),
        config_schema={
            "$schema": SCHEMA,
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "base_url": {
                    "type": "string",
                    "format": "uri",
                    "default": "https://api.anthropic.com",
                },
                "anthropic_version": {"type": "string", "default": "2023-06-01"},
            },
        },
        secret_fields=("api_key", "token"),
        destination_fields=("base_url",),
        auth_methods=("api_key",),
        connection_type="model_api",
        declared_maturity="experimental",
    )

    def _client(self, ctx: ExecContext, conn: ConnectionConfig) -> httpx.Client:
        headers = auth_headers(conn, header_name="x-api-key")
        headers["anthropic-version"] = str(conn.config.get("anthropic_version", "2023-06-01"))
        return ctx.egress.client(headers=headers)

    def _request(
        self,
        ctx: ExecContext,
        conn: ConnectionConfig,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        merged = ConnectionConfig(
            conn.id,
            conn.name,
            conn.adapter_key,
            {**conn.config, "base_url": conn.config.get("base_url", "https://api.anthropic.com")},
            conn.auth_method,
            conn.access_mode,
            conn.credential,
        )
        return super()._request(ctx, merged, method, path, body)

    def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome:
        conn = ConnectionConfig(
            conn.id,
            conn.name,
            conn.adapter_key,
            {**conn.config, "base_url": conn.config.get("base_url", "https://api.anthropic.com")},
            conn.auth_method,
            conn.access_mode,
            conn.credential,
        )
        return super().test(ctx, conn)

    def generate(
        self, ctx: ExecContext, conn: ConnectionConfig, model_key: str, request: ChatRequest
    ) -> ChatResponse:
        system = "\n".join(m.content for m in request.messages if m.role == "system")
        max_tokens = _checked_max_tokens(request.max_tokens)
        payload: dict[str, Any] = {
            "model": model_key,
            "max_tokens": 1024 if max_tokens is None else max_tokens,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in request.messages
                if m.role in ("user", "assistant")
            ],
        }
        if system:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        response = self._request(ctx, conn, "POST", "/v1/messages", payload)
        try:
            doc = response.json()
            text = "".join(p.get("text", "") for p in doc["content"] if p.get("type") == "text")
            usage = doc.get("usage") or {}
            return ChatResponse(
                output=text,
                usage=Usage(
                    _tokens(usage.get("input_tokens")),
                    _tokens(usage.get("output_tokens")),
                    _tokens(usage.get("cache_read_input_tokens")),
                ),
                finish_reason=doc.get("stop_reason"),
                provider_request_id=response.headers.get("request-id") or doc.get("id"),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterFailure(
                ErrorClass.UPSTREAM, "the provider returned an unexpected body"
            ) from exc

    def list_models(self, ctx: ExecContext, conn: ConnectionConfig) -> list[ModelInfo]:
        response = self._request(ctx, conn, "GET", "/v1/models")
        try:
            return [
                ModelInfo(str(m["id"]), m.get("display_name") or str(m["id"]))
                for m in response.json()["data"]
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterFailure(
                ErrorClass.UPSTREAM, "the provider returned an unexpected body"
            ) from exc
