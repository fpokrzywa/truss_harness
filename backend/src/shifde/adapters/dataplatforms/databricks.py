"""`databricks` connection adapter: Unity Catalog discovery and SQL over the Statement Execution API
(design 2026-11-22 section 7.2, B-2753).

Auth is a bearer token (PAT or service principal token) or OAuth M2M (`oauth2.OAuth2Tokens`
against `<host>/oidc/v1/token`, scope `all-apis`, cached per client, connection and secret
fingerprint). Every request goes through `ctx.egress`; the module never reads settings: the
installation's extra host suffixes reach `normalize_config` as `AdapterRules`.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, ClassVar  # Any: decoded JSON bodies, result values

import httpx

from shifde.adapters.base import (
    AdapterDescriptor,
    AdapterFailure,
    AdapterRules,
    Capability,
    CheckRecorder,
    ConfigError,
    ConfigRefused,
    ConnectionConfig,
    Credential,
    DiscoveryResult,
    ErrorClass,
    ExecContext,
    InvokeResult,
    ProbeOutcome,
    validate_against_schema,
)
from shifde.adapters.dataplatforms import sqlapi
from shifde.adapters.httputil import classify_exception, standard_network_checks
from shifde.adapters.native.addresses import AddressRefused, normalize_databricks_address
from shifde.adapters.oauth2 import AUTH_METHOD as OAUTH2
from shifde.adapters.oauth2 import OAuth2Tokens

NAME_PATTERN = "^[A-Za-z0-9_][A-Za-z0-9_-]{0,254}$"
HOST = re.compile(r"^https://[a-z0-9.-]+$")
DEFAULT_MAX_ROWS = 1000
DEFAULT_STATEMENT_TIMEOUT_S = 30
MAX_CATALOGS = 50
MAX_SCHEMAS_PER_CATALOG = 200
MAX_TABLES = 2000
TOKEN_PATH = "/oidc/v1/token"
OAUTH_SCOPE = "all-apis"
ME = "/api/2.0/preview/scim/v2/Me"
CATALOGS = "/api/2.1/unity-catalog/catalogs"
SCHEMAS = "/api/2.1/unity-catalog/schemas"
TABLES = "/api/2.1/unity-catalog/tables"
WAREHOUSES = "/api/2.0/sql/warehouses"
STATEMENTS = "/api/2.0/sql/statements"
PENDING_STATES = frozenset({"PENDING", "RUNNING"})
_STATE = re.compile(r"[A-Z_]{1,32}")

NEEDS_CREDENTIAL = "a Databricks connection needs a credential"
NEEDS_CLIENT_ID = "client_id is required for oauth2_client_credentials"
NEEDS_WAREHOUSE = "warehouse_id is required to run statements"
BAD_CREDENTIAL = "the credential must be a non-empty value without whitespace"
STORED_HOST = "the stored host is not a workspace URL of the form https://<host>; update it"
SUBJECT = "the Databricks workspace"

JSON = dict[str, Any]  # Any: free-form JSON object


class _Auth:
    """The connection's credential as request headers; under OAuth2 also the refresh."""

    def __init__(self, ctx: ExecContext, conn: ConnectionConfig, host: str) -> None:
        self._bearer: Credential | None = None
        self._oauth2: OAuth2Tokens | None = None
        if conn.auth_method == OAUTH2:
            client_id = conn.config.get("client_id")
            if not client_id:
                raise AdapterFailure(ErrorClass.CONFIG, NEEDS_CLIENT_ID)
            derived = dataclasses.replace(
                conn,
                config={
                    "oauth2_url": f"{host}{TOKEN_PATH}",
                    "client_id": str(client_id),
                    "scope": OAUTH_SCOPE,
                },
            )
            self._oauth2 = OAuth2Tokens(ctx, derived)
        elif conn.auth_method == "bearer":
            if conn.credential is None:
                raise AdapterFailure(ErrorClass.CONFIG, NEEDS_CREDENTIAL)
            self._bearer = conn.credential

    def __repr__(self) -> str:
        return "DatabricksAuth(***)"

    def headers(self) -> dict[str, str]:
        if self._oauth2 is not None:
            return {"Authorization": f"Bearer {self._oauth2.token()}"}
        if self._bearer is not None:
            return {"Authorization": f"Bearer {self._bearer.reveal()}"}
        return {}

    def refreshed(self) -> dict[str, str] | None:
        """New headers after a 401 (OAuth2 only), else None."""
        if self._oauth2 is None:
            return None
        return {"Authorization": f"Bearer {self._oauth2.refresh()}"}


def _stored_host(config: JSON) -> str:
    """The stored `host` when it is in normalized `https://<host>` form, else CONFIG (B-3116).

    A row stored before this adapter (the planned one took any string) reaches discover and
    invoke unchecked, so the session refuses anything `normalize_config` could not have stored:
    a scheme but https, a port, user info, a path, an IP literal or a malformed label. The suffix
    allowlist is not repeated here; its extra suffixes are installation settings."""
    host = str(config.get("host", ""))
    _, dot, tail = host.partition(".")
    try:
        normalized = bool(dot) and normalize_databricks_address(host, (tail,)) == host
    except AddressRefused:
        normalized = False
    if not normalized:
        raise AdapterFailure(ErrorClass.CONFIG, STORED_HOST)
    return host


class DatabricksSession:
    """One adapter session: an egress-guarded client and the connection's credential."""

    def __init__(self, ctx: ExecContext, conn: ConnectionConfig) -> None:
        self._ctx = ctx
        self._conn = conn
        self._host = _stored_host(conn.config)
        self._auth = _Auth(ctx, conn, self._host)
        self._client = ctx.egress.client(headers={"Accept": "application/json"})

    def __repr__(self) -> str:
        return f"DatabricksSession({self._host})"

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:  # Any: httpx
        """One call; under OAuth2 a 401 is retried once with a refreshed token."""
        url = f"{self._host}{path}"
        response = sqlapi.send(
            self._ctx, self._client, method, url, headers=self._auth.headers(), **kwargs
        )
        if response.status_code == 401:
            headers = self._auth.refreshed()
            if headers is not None:
                response = sqlapi.send(
                    self._ctx, self._client, method, url, headers=headers, **kwargs
                )
        return response

    def json(self, method: str, path: str, **kwargs: Any) -> JSON:  # Any: httpx options
        response = self.request(method, path, **kwargs)
        failure = sqlapi.classify_status(response.status_code, subject=SUBJECT)
        if failure is not None:
            raise failure
        try:
            doc = response.json()
        except ValueError as exc:
            raise AdapterFailure(ErrorClass.UPSTREAM, f"{SUBJECT} answer is not JSON") from exc
        if not isinstance(doc, dict):
            raise AdapterFailure(ErrorClass.UPSTREAM, f"{SUBJECT} answer is not a JSON object")
        return doc

    def paged(
        self, path: str, params: dict[str, str], key: str, cap: int
    ) -> tuple[list[JSON], bool]:
        """Items of a Unity Catalog list, following `next_page_token` up to `cap` items; the flag
        is True when more were available."""
        items: list[JSON] = []
        token: str | None = None
        while True:
            query = {**params, "max_results": str(cap)} | ({"page_token": token} if token else {})
            doc = self.json("GET", path, params=query)
            page = [i for i in doc.get(key) or [] if isinstance(i, dict)]
            items.extend(page)
            token = doc.get("next_page_token") or None
            if len(items) > cap:
                return items[:cap], True
            if not token or not page:
                return items, False
            if len(items) == cap:
                return items, True

    # ------------------------------------------------------------------ invoke
    def invoke(self, request: JSON, *, write: bool = False) -> InvokeResult:
        try:
            return self._invoke(request, write)
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            raise AdapterFailure(ErrorClass.CONFIG, "malformed request") from exc

    def _invoke(self, request: JSON, write: bool) -> InvokeResult:
        cfg = self._conn.config
        warehouse = cfg.get("warehouse_id")
        if not warehouse:
            raise AdapterFailure(ErrorClass.CONFIG, NEEDS_WAREHOUSE)
        statement = str(request.get("sql") or "")
        if not statement.strip():
            raise AdapterFailure(ErrorClass.CONFIG, "request needs `sql`")
        if not write:
            sqlapi.check_read_only(statement)
        params = request.get("params")
        if isinstance(params, list):
            raise AdapterFailure(
                ErrorClass.CONFIG,
                "Databricks takes named parameters only: send params as an object",
            )
        max_rows = int(cfg.get("max_rows", DEFAULT_MAX_ROWS))
        wanted = request.get("max_rows")
        if wanted is not None:
            if isinstance(wanted, bool) or not isinstance(wanted, int) or wanted < 1:
                raise AdapterFailure(ErrorClass.CONFIG, "malformed request")
            max_rows = min(wanted, max_rows)
        types = request.get("param_types") or {}
        if not isinstance(types, dict):
            raise AdapterFailure(ErrorClass.CONFIG, "malformed request")
        timeout = int(cfg.get("statement_timeout_seconds", DEFAULT_STATEMENT_TIMEOUT_S))
        body: JSON = {
            "warehouse_id": warehouse,
            "statement": statement,
            "wait_timeout": f"{timeout}s",
            "on_wait_timeout": "CANCEL",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
            "row_limit": max_rows + 1,
        }
        if params:
            body["parameters"] = [
                {"name": str(k), "value": None if v is None else str(v)}
                | ({"type": str(types[k])} if k in types else {})
                for k, v in params.items()
            ]
        if cfg.get("default_catalog"):
            body["catalog"] = cfg["default_catalog"]
        if cfg.get("default_schema"):
            body["schema"] = cfg["default_schema"]
        doc = sqlapi.run_statement(
            self._ctx,
            sqlapi.StatementCalls(
                submit=lambda: self.json("POST", STATEMENTS, json=body),
                poll=lambda sid: self.json("GET", f"{STATEMENTS}/{sid}"),
                cancel=lambda sid: self.request("POST", f"{STATEMENTS}/{sid}/cancel").close(),
                statement_id=lambda d: str(d.get("statement_id") or "") or None,
                pending=lambda d: _state(d) in PENDING_STATES,
            ),
            timeout_seconds=timeout,
        )
        state = _state(doc)
        if state == "FAILED":
            raise AdapterFailure(ErrorClass.UPSTREAM, "the statement failed on the warehouse")
        if state in ("CANCELED", "CLOSED"):
            raise AdapterFailure(
                ErrorClass.TIMEOUT, "the statement was cancelled or closed before it finished"
            )
        if state != "SUCCEEDED":
            raise AdapterFailure(ErrorClass.UPSTREAM, "the statement ended in an unknown state")
        manifest = doc.get("manifest") or {}
        result = doc.get("result") or {}
        columns = [str(c.get("name")) for c in (manifest.get("schema") or {}).get("columns") or []]
        rows, capped = sqlapi.cap_rows(result.get("data_array") or [], max_rows)
        truncated = capped or bool(manifest.get("truncated")) or "next_chunk_index" in result
        return InvokeResult(
            {"columns": columns, "rows": rows, "row_count": len(rows)}, truncated=truncated
        )

    def close(self) -> None:
        self._client.close()


def _state(doc: JSON) -> str:
    status = doc.get("status")
    return str(status.get("state") or "") if isinstance(status, dict) else ""


class DatabricksAdapter:
    descriptor: ClassVar[AdapterDescriptor] = AdapterDescriptor(
        key="databricks",
        kind="connection",
        version="1.0.0",
        display_name="Databricks SQL",
        capabilities=frozenset({Capability.QUERY_SQL, Capability.DISCOVER_SCHEMA}),
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["host"],
            "properties": {
                "host": {"type": "string", "title": "Workspace URL"},
                "warehouse_id": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9-]{1,64}$",
                    "title": "SQL warehouse id",
                },
                "default_catalog": {
                    "type": "string",
                    "pattern": NAME_PATTERN,
                    "title": "Default catalog",
                },
                "default_schema": {
                    "type": "string",
                    "pattern": NAME_PATTERN,
                    "title": "Default schema",
                },
                "catalogs": {
                    "type": "array",
                    "items": {"type": "string", "pattern": NAME_PATTERN},
                    "maxItems": MAX_CATALOGS,
                    "title": "Catalogs to discover",
                },
                "client_id": {"type": "string", "title": "OAuth client id (service principal)"},
                "max_rows": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10_000,
                    "default": DEFAULT_MAX_ROWS,
                    "title": "Maximum rows per statement",
                },
                "statement_timeout_seconds": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 50,
                    "default": DEFAULT_STATEMENT_TIMEOUT_S,
                    "title": "Statement timeout (seconds)",
                },
            },
        },
        secret_fields=("token", "client_secret"),
        destination_fields=("host",),
        auth_methods=("none", "bearer", OAUTH2),
        auth_only_fields={"client_id": (OAUTH2,)},
        connection_type="data_platform",
        declared_maturity="experimental",
    )

    # ------------------------------------------------------------------ hooks (B-2753)
    def normalize_config(self, config: JSON, rules: AdapterRules) -> JSON:
        """A copy whose `host` is the normalized `https://<host>` (a bare host is read as
        https); a refused address raises `ConfigRefused` with one fixed message."""
        host = config.get("host")
        if not isinstance(host, str):
            return dict(config)  # the schema check reports it
        address = host if "://" in host else f"https://{host}"
        try:
            normalized = normalize_databricks_address(address, rules.databricks_host_suffixes)
        except AddressRefused as exc:
            raise ConfigRefused([ConfigError("host", str(exc))]) from exc
        return {**config, "host": normalized}

    def validate_credential(self, auth_method: str, value: str) -> list[ConfigError]:
        if auth_method not in ("bearer", OAUTH2):
            return []
        if not value.strip() or any(c.isspace() or not c.isprintable() for c in value):
            return [ConfigError("value", BAD_CREDENTIAL)]
        return []

    def validate_config(self, config: JSON) -> list[ConfigError]:
        def extra(cfg: JSON) -> list[ConfigError]:
            if not HOST.fullmatch(str(cfg.get("host", ""))):
                return [ConfigError("host", "must be a workspace URL of the form https://<host>")]
            return []

        return validate_against_schema(self.descriptor, config, extra=extra)

    # ------------------------------------------------------------------ test / discover / open
    def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome:
        rec = CheckRecorder()
        errors = self.validate_config(conn.config)
        if errors:
            rec.fail("config", "; ".join(f"{e.path}: {e.message}" for e in errors)[:400])
            return ProbeOutcome(rec.finish(), ErrorClass.CONFIG, "configuration is invalid")
        if conn.auth_method == OAUTH2 and not conn.config.get("client_id"):
            rec.fail("config", NEEDS_CLIENT_ID)
            return ProbeOutcome(rec.finish(), ErrorClass.CONFIG, "configuration is invalid")
        rec.ok("config", "configuration is valid")
        if not standard_network_checks(ctx, str(conn.config["host"]), rec):
            return ProbeOutcome(rec.finish(), ErrorClass.EGRESS, "destination denied by policy")
        if conn.auth_method == "none" or conn.credential is None:
            rec.fail("auth", NEEDS_CREDENTIAL)
            return ProbeOutcome(rec.finish(), ErrorClass.AUTH, NEEDS_CREDENTIAL)
        try:
            session = DatabricksSession(ctx, conn)
        except AdapterFailure as exc:
            rec.fail("config", exc.message)
            return ProbeOutcome(rec.finish(), exc.error_class, exc.message)
        try:
            return self._probe(session, conn, rec)
        finally:
            session.close()

    def _probe(
        self, session: DatabricksSession, conn: ConnectionConfig, rec: CheckRecorder
    ) -> ProbeOutcome:
        try:
            response = session.request("GET", ME)
        except Exception as exc:  # noqa: BLE001 - classified below, never leaks the message
            failure = classify_exception(exc)
            name = {ErrorClass.TLS: "tls", ErrorClass.DNS: "dns"}.get(failure.error_class, "auth")
            rec.fail(name, failure.message)
            return ProbeOutcome(rec.finish(), failure.error_class, failure.message)
        rec.ok("tls", "certificate verified for the requested host name")
        refused = sqlapi.classify_status(response.status_code, subject=SUBJECT)
        if refused is not None:
            rec.fail("auth", refused.message)
            return ProbeOutcome(rec.finish(), refused.error_class, refused.message)
        rec.ok("auth", "credentials accepted")
        try:
            session.json("GET", CATALOGS, params={"max_results": "1"})
            detail = "Unity Catalog reachable"
            warehouse = conn.config.get("warehouse_id")
            if warehouse:
                try:
                    doc = session.json("GET", f"{WAREHOUSES}/{warehouse}")
                except AdapterFailure as exc:
                    if exc.error_class is ErrorClass.CONFIG:
                        raise AdapterFailure(
                            ErrorClass.CONFIG, "the SQL warehouse was not found (HTTP 404)"
                        ) from exc
                    raise
                state = str(doc.get("state") or "")
                detail += f"; SQL warehouse state {state if _STATE.fullmatch(state) else 'unknown'}"
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            failure = classify_exception(exc)
            rec.fail("capability", failure.message)
            return ProbeOutcome(rec.finish(), failure.error_class, failure.message)
        rec.ok("capability", detail)
        return ProbeOutcome(rec.finish())

    def discover(self, ctx: ExecContext, conn: ConnectionConfig) -> DiscoveryResult:
        session = DatabricksSession(ctx, conn)
        try:
            return self._discover(session, conn)
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            raise classify_exception(exc) from exc
        finally:
            session.close()

    def _discover(self, session: DatabricksSession, conn: ConnectionConfig) -> DiscoveryResult:
        truncated = False
        listed = conn.config.get("catalogs")
        if listed:
            catalogs = [str(c) for c in listed][:MAX_CATALOGS]
        else:
            found, more = session.paged(CATALOGS, {}, "catalogs", MAX_CATALOGS)
            catalogs, truncated = [str(c.get("name")) for c in found], more
        tables: list[JSON] = []
        for catalog in catalogs:
            schemas, more = session.paged(
                SCHEMAS, {"catalog_name": catalog}, "schemas", MAX_SCHEMAS_PER_CATALOG
            )
            truncated = truncated or more
            for schema in (str(s.get("name")) for s in schemas):
                left = MAX_TABLES - len(tables)
                if left <= 0:
                    return DiscoveryResult("schema", {"tables": tables, "truncated": True})
                found, more = session.paged(
                    TABLES, {"catalog_name": catalog, "schema_name": schema}, "tables", left
                )
                truncated = truncated or more
                tables.extend(_table(catalog, schema, t) for t in found)
        return DiscoveryResult("schema", {"tables": tables, "truncated": truncated})

    def open(self, ctx: ExecContext, conn: ConnectionConfig) -> DatabricksSession:
        return DatabricksSession(ctx, conn)


def _table(catalog: str, schema: str, doc: JSON) -> JSON:
    return {
        "catalog": catalog,
        "schema": schema,
        "name": str(doc.get("name")),
        "table_type": str(doc.get("table_type") or ""),
        "columns": [
            {
                "name": str(c.get("name")),
                "type": str(c.get("type_text") or c.get("type_name") or ""),
                "nullable": bool(c.get("nullable", True)),
            }
            for c in doc.get("columns") or []
            if isinstance(c, dict)
        ],
    }
