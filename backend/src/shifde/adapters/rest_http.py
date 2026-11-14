"""`rest.http` connection adapter: governed calls to an HTTP/JSON API through the EgressGuard."""

from __future__ import annotations

from typing import Any, ClassVar  # Any: decoded JSON bodies

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
from shifde.adapters.httputil import (
    FORBIDDEN_HEADERS,
    auth_headers,
    body_preview,
    check_base_url,
    classify_exception,
    join_url,
    standard_network_checks,
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RestHttpSession:
    def __init__(self, ctx: ExecContext, conn: ConnectionConfig) -> None:
        self._ctx = ctx
        self._conn = conn
        self._client = ctx.egress.client(
            headers={**conn.config.get("default_headers", {}), **auth_headers(conn)}
        )

    def invoke(self, request: dict[str, Any], *, write: bool = False) -> InvokeResult:
        try:
            return self._invoke(request, write)
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            raise AdapterFailure(ErrorClass.CONFIG, "malformed request") from exc

    def _invoke(self, request: dict[str, Any], write: bool) -> InvokeResult:
        method = str(request.get("method", "GET")).upper()
        if method not in SAFE_METHODS | WRITE_METHODS:
            raise AdapterFailure(ErrorClass.CONFIG, f"method {method!r} is not supported")
        if method in WRITE_METHODS and not write:
            raise AdapterFailure(
                ErrorClass.CAPABILITY,
                "this call can change data and needs read_write access plus connection:write",
            )
        url = join_url(str(self._conn.config["base_url"]), str(request.get("path", "")))
        headers = {
            k: str(v)
            for k, v in (request.get("headers") or {}).items()
            if str(k).lower() not in FORBIDDEN_HEADERS
        }
        try:
            response = self._client.request(
                method,
                url,
                params=request.get("query"),
                json=request.get("body") if request.get("body") is not None else None,
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            raise classify_exception(exc) from exc
        body, truncated = body_preview(response)
        return InvokeResult(
            result={
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "body": body,
            },
            truncated=truncated,
        )

    def close(self) -> None:
        self._client.close()


class RestHttpAdapter:
    descriptor: ClassVar[AdapterDescriptor] = AdapterDescriptor(
        key="rest.http",
        kind="connection",
        version="1.0.0",
        display_name="REST / HTTP API",
        capabilities=frozenset({Capability.REST_CALL, Capability.DISCOVER_OPENAPI}),
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["base_url"],
            "properties": {
                "base_url": {"type": "string", "format": "uri", "title": "Base URL"},
                "health_path": {"type": "string", "default": "/", "title": "Health check path"},
                "openapi_path": {
                    "type": "string",
                    "default": "/openapi.json",
                    "title": "OpenAPI document path",
                },
                "username": {"type": "string", "title": "Username (basic auth)"},
                "api_key_header": {"type": "string", "default": "X-API-Key"},
                "default_headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "title": "Default (non-secret) headers",
                },
            },
        },
        secret_fields=("token", "api_key", "password"),
        destination_fields=("base_url",),
        auth_methods=("none", "api_key", "bearer", "basic"),
        connection_type="rest",
    )

    def validate_config(self, config: dict[str, Any]) -> list[ConfigError]:
        def extra(cfg: dict[str, Any]) -> list[ConfigError]:
            errs: list[ConfigError] = []
            base = str(cfg.get("base_url", ""))
            if not base.startswith(("http://", "https://")):
                errs.append(ConfigError("base_url", "must be an absolute http(s) URL"))
            elif "@" in base.split("//", 1)[1].split("/", 1)[0]:
                errs.append(ConfigError("base_url", "credentials in the URL are not allowed"))
            if not errs:
                try:
                    check_base_url(base)
                except AdapterFailure as exc:
                    errs.append(ConfigError("base_url", exc.message))
            if not errs:
                for key in ("health_path", "openapi_path"):
                    if key in cfg:  # the same check a call makes, so a bad path fails here
                        try:
                            join_url(base, str(cfg[key]))
                        except AdapterFailure as exc:
                            errs.append(ConfigError(key, exc.message))
            if str(cfg.get("api_key_header", "")).lower() in FORBIDDEN_HEADERS:
                errs.append(ConfigError("api_key_header", "header is not allowed"))
            for name in cfg.get("default_headers", {}):
                if str(name).lower() in FORBIDDEN_HEADERS:
                    errs.append(ConfigError(f"default_headers/{name}", "header is not allowed"))
            return errs

        return validate_against_schema(self.descriptor, config, extra=extra)

    # ------------------------------------------------------------------ test / discover / open
    def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome:
        rec = CheckRecorder()
        errors = self.validate_config(conn.config)
        if errors:
            rec.fail("config", "; ".join(f"{e.path}: {e.message}" for e in errors)[:400])
            return ProbeOutcome(rec.finish(), ErrorClass.CONFIG, "configuration is invalid")
        rec.ok("config", "configuration is valid")
        base = str(conn.config["base_url"])
        if not standard_network_checks(ctx, base, rec):
            return ProbeOutcome(rec.finish(), ErrorClass.EGRESS, "destination denied by policy")
        try:
            url = join_url(base, str(conn.config.get("health_path", "/")))
            session = RestHttpSession(ctx, conn)
        except AdapterFailure as exc:
            rec.fail("capability", exc.message)
            return ProbeOutcome(rec.finish(), exc.error_class, exc.message)
        try:
            response = session._client.get(url)
        except Exception as exc:  # noqa: BLE001 - classified below, never leaks the message
            failure = classify_exception(exc)
            name = {ErrorClass.TLS: "tls", ErrorClass.DNS: "dns"}.get(failure.error_class)
            rec.fail(name or "capability", failure.message)
            return ProbeOutcome(rec.finish(), failure.error_class, failure.message)
        finally:
            session.close()
        if base.startswith("https://"):
            rec.ok("tls", "certificate verified for the requested host name")
        else:
            rec.skip("tls", "plain http")
        if response.status_code in (401, 403):
            rec.fail("auth", f"the endpoint rejected the credentials (HTTP {response.status_code})")
            return ProbeOutcome(rec.finish(), ErrorClass.AUTH, "authentication failed")
        rec.ok("auth", "credentials accepted" if conn.credential else "no credentials configured")
        if response.status_code >= 500:
            rec.fail("capability", f"the endpoint answered HTTP {response.status_code}")
            return ProbeOutcome(rec.finish(), ErrorClass.UPSTREAM, "endpoint error")
        rec.ok("capability", f"HTTP {response.status_code} from the health path")
        return ProbeOutcome(rec.finish())

    def discover(self, ctx: ExecContext, conn: ConnectionConfig) -> DiscoveryResult:
        session = RestHttpSession(ctx, conn)
        try:
            url = join_url(
                str(conn.config["base_url"]), str(conn.config.get("openapi_path", "/openapi.json"))
            )
            response = session._client.get(url)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from exc
        finally:
            session.close()
        if response.status_code != 200:
            raise AdapterFailure(
                ErrorClass.CAPABILITY, f"no OpenAPI document (HTTP {response.status_code})"
            )
        try:
            doc = response.json()
        except ValueError as exc:
            raise AdapterFailure(ErrorClass.CAPABILITY, "the OpenAPI document is not JSON") from exc
        paths: list[dict[str, Any]] = []
        for path, item in (doc.get("paths") or {}).items():
            for method, op in (item or {}).items():
                if method.lower() in {"get", "post", "put", "patch", "delete", "head"}:
                    paths.append(
                        {
                            "method": method.upper(),
                            "path": path,
                            "summary": (op or {}).get("summary") if isinstance(op, dict) else None,
                        }
                    )
        return DiscoveryResult("openapi", {"paths": paths[:1000]})

    def open(self, ctx: ExecContext, conn: ConnectionConfig) -> RestHttpSession:
        return RestHttpSession(ctx, conn)
