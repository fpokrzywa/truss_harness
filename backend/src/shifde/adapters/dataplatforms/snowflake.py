"""`snowflake` connection adapter: discovery and SQL over the Snowflake SQL API v2 (design
2026-11-22 section 7.3).

Auth is `key_pair` (an RS256 JWT signed here with `cryptography`, cached per client, connection and
key fingerprint) or `bearer` (a programmatic access token). The host is derived from `account`
and `.snowflakecomputing.com` is the only suffix. Every request goes through `ctx.egress`; the
module never reads settings.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any, ClassVar  # Any: decoded JSON bodies, result values
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from shifde.adapters import oauth2
from shifde.adapters.base import (
    AdapterDescriptor,
    AdapterFailure,
    Capability,
    CheckRecorder,
    ConfigError,
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
from shifde.core.redaction import register_secret

ACCOUNT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,254}(\.[A-Za-z0-9-]{1,64}){0,2}$"
ACCOUNT = re.compile(ACCOUNT_PATTERN)
HOST_SUFFIX = ".snowflakecomputing.com"
DEFAULT_MAX_ROWS = 1000
DEFAULT_STATEMENT_TIMEOUT_S = 30
MAX_DATABASES = 50
MAX_COLUMN_ROWS = 20_000
JWT_LIFETIME_S = 300
MIN_KEY_BITS = 2048
STATEMENTS = "/api/v2/statements"
TEST_STATEMENT = "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_VERSION()"
SHOW_DATABASES = "SHOW DATABASES"
_HANDLE = re.compile(r"[A-Za-z0-9-]{1,128}")
_VERSION = re.compile(r"[0-9][0-9.]{0,31}")

PEM_PKCS8 = "-----BEGIN PRIVATE KEY-----"  # scan-secrets: allow, a PEM header, no key material
PEM_ENCRYPTED = "-----BEGIN ENCRYPTED PRIVATE KEY-----"  # scan-secrets: allow, a PEM header
PEM_PKCS1 = "-----BEGIN RSA PRIVATE KEY-----"  # scan-secrets: allow, a PEM header

KEY_PAIR = "key_pair"
TOKEN_TYPE = {KEY_PAIR: "KEYPAIR_JWT", "bearer": "PROGRAMMATIC_ACCESS_TOKEN"}

NEEDS_CREDENTIAL = "a Snowflake connection needs a credential"
NEEDS_USER = "user is required"
BAD_ACCOUNT = "the stored account is not a Snowflake account identifier; update it"
BAD_HOST = "the account does not give a host under .snowflakecomputing.com"
BAD_AUTH = "a Snowflake connection uses key_pair or bearer auth"
NOT_PKCS8 = "the private key must be an unencrypted PKCS#8 PEM (BEGIN PRIVATE KEY)"
ENCRYPTED = "passphrase-protected private keys are not supported"
PKCS1 = "PKCS#1 keys (BEGIN RSA PRIVATE KEY) are not supported; convert the key to PKCS#8"
UNREADABLE = "the private key could not be read as a PEM private key"
NOT_RSA = "the private key must be an RSA key"
TOO_SHORT = f"the RSA key must have at least {MIN_KEY_BITS} bits"
EMPTY_TOKEN = "the programmatic access token must not be empty"
SUBJECT = "the Snowflake account"

JSON = dict[str, Any]  # Any: free-form JSON object

clock = time.time  # wall clock of the JWT `iat`; patched by tests


def host_of(account: str) -> str:
    """`https://<account lowercased, "_" to "-">.snowflakecomputing.com`, or CONFIG when the
    account is no account identifier or the host would leave the suffix."""
    if not ACCOUNT.fullmatch(account):
        raise AdapterFailure(ErrorClass.CONFIG, BAD_ACCOUNT)
    url = f"https://{account.lower().replace('_', '-')}{HOST_SUFFIX}"
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if parts.netloc != hostname or not hostname.endswith(HOST_SUFFIX):
        raise AdapterFailure(ErrorClass.CONFIG, BAD_HOST)
    return url


def _private_key(value: str) -> rsa.RSAPrivateKey | str:
    """The RSA key of an unencrypted PKCS#8 PEM, or the fixed reason it is refused."""
    text = value.strip()
    if text.startswith(PEM_ENCRYPTED):
        return ENCRYPTED
    if text.startswith(PEM_PKCS1):
        return PKCS1
    if not text.startswith(PEM_PKCS8):
        return NOT_PKCS8
    try:
        key = serialization.load_pem_private_key(text.encode(), password=None)
    except TypeError:
        return ENCRYPTED
    except (ValueError, UnsupportedAlgorithm):
        return UNREADABLE
    if not isinstance(key, rsa.RSAPrivateKey):
        return NOT_RSA
    if key.key_size < MIN_KEY_BITS:
        return TOO_SHORT
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_key_fingerprint(key: rsa.RSAPublicKey) -> str:
    """`SHA256:<standard base64 of sha256(DER SubjectPublicKeyInfo)>`."""
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()


def jwt_subject(account: str, user: str) -> str:
    """`<ACCOUNT>.<USER>`: both uppercased, the account cut to its locator when a region follows."""
    return f"{account.split('.', 1)[0].upper()}.{user.upper()}"


class KeyPairJwt:
    """Key-pair JWTs for one connection in one execution context."""

    def __init__(self, ctx: ExecContext, conn: ConnectionConfig, credential: Credential) -> None:
        key = _private_key(credential.reveal())
        if isinstance(key, str):
            raise AdapterFailure(ErrorClass.CONFIG, key)
        self._key = key
        account, user = str(conn.config["account"]), str(conn.config["user"])
        self._sub = jwt_subject(account, user)
        self._iss = f"{self._sub}.{public_key_fingerprint(key.public_key())}"
        self._cache_key: oauth2.CacheKey | None = None
        if ctx.tenant_id is not None and conn.id is not None:
            material = json.dumps([credential.reveal(), account, user])
            fingerprint = hashlib.sha256(material.encode()).hexdigest()
            self._cache_key = (ctx.tenant_id, conn.id, fingerprint)

    def __repr__(self) -> str:
        return "KeyPairJwt(***)"

    def token(self) -> str:
        """A valid JWT: the cached one, or a freshly signed one."""
        if self._cache_key is not None:
            cached = oauth2.TOKEN_CACHE.get(self._cache_key)
            if cached is not None:
                return cached
        return self._sign()

    def refresh(self) -> str:
        """Drop the cached JWT (the account refused it) and sign a new one."""
        if self._cache_key is not None:
            oauth2.TOKEN_CACHE.drop(self._cache_key)
        return self._sign()

    def _sign(self) -> str:
        iat = int(clock())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = {"iss": self._iss, "sub": self._sub, "iat": iat, "exp": iat + JWT_LIFETIME_S}
        payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
        signing_input = f"{header}.{payload}".encode()
        signature = self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        token = f"{header}.{payload}.{_b64url(signature)}"
        register_secret(token)
        if self._cache_key is not None:
            expires = oauth2.clock() + JWT_LIFETIME_S - oauth2.EXPIRY_MARGIN_S
            oauth2.TOKEN_CACHE.put(self._cache_key, token, expires)
        return token


class _Auth:
    """The connection's credential as request headers; under `key_pair` also the re-sign."""

    def __init__(self, ctx: ExecContext, conn: ConnectionConfig) -> None:
        if conn.auth_method not in TOKEN_TYPE:
            raise AdapterFailure(ErrorClass.CONFIG, BAD_AUTH)
        if conn.credential is None:
            raise AdapterFailure(ErrorClass.CONFIG, NEEDS_CREDENTIAL)
        self._type = TOKEN_TYPE[conn.auth_method]
        self._pat: Credential | None = None
        self._jwt: KeyPairJwt | None = None
        if conn.auth_method == KEY_PAIR:
            self._jwt = KeyPairJwt(ctx, conn, conn.credential)
        else:
            self._pat = conn.credential

    def __repr__(self) -> str:
        return "SnowflakeAuth(***)"

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": self._type,
        }

    def headers(self) -> dict[str, str]:
        if self._jwt is not None:
            return self._headers(self._jwt.token())
        return self._headers(self._pat.reveal() if self._pat is not None else "")

    def refreshed(self) -> dict[str, str] | None:
        """New headers after a 401 (`key_pair` only), else None."""
        if self._jwt is None:
            return None
        return self._headers(self._jwt.refresh())


def _stored_account(config: JSON) -> tuple[str, str]:
    """(host, user) of a stored config; CONFIG for anything the schema could not have stored (a
    row of the planned adapter took any account)."""
    host = host_of(str(config.get("account", "")))
    user = config.get("user")
    if not isinstance(user, str) or not user:
        raise AdapterFailure(ErrorClass.CONFIG, NEEDS_USER)
    return host, user


def _binding(value: Any) -> JSON:  # Any: a bound parameter value
    """One positional binding: `{"type", "value"}` with the value as text."""
    if value is None:
        return {"type": "TEXT", "value": None}
    if isinstance(value, bool):
        return {"type": "BOOLEAN", "value": "true" if value else "false"}
    if isinstance(value, int):
        return {"type": "FIXED", "value": str(value)}
    if isinstance(value, float):
        return {"type": "REAL", "value": repr(value)}
    if isinstance(value, str):
        return {"type": "TEXT", "value": value}
    raise AdapterFailure(ErrorClass.CONFIG, "parameters must be strings, numbers, booleans or null")


class SnowflakeSession:
    """One adapter session: an egress-guarded client and the connection's credential."""

    def __init__(self, ctx: ExecContext, conn: ConnectionConfig) -> None:
        self._ctx = ctx
        self._conn = conn
        self._host, _ = _stored_account(conn.config)
        self._auth = _Auth(ctx, conn)
        self._client = ctx.egress.client(
            headers={"Accept": "application/json", "Content-Type": "application/json"}
        )

    def __repr__(self) -> str:
        return f"SnowflakeSession({self._host})"

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:  # Any: httpx
        """One call; under `key_pair` a 401 is retried once with a newly signed JWT."""
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

    def statement_doc(self, method: str, path: str, **kwargs: Any) -> JSON:  # Any: httpx
        """`{"pending": bool, "doc": <answer>}` of a statement call (202 is still running)."""
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
        return {"pending": response.status_code == 202, "doc": doc}

    def body(self, statement: str, bindings: JSON | None = None) -> JSON:
        cfg = self._conn.config
        body: JSON = {
            "statement": statement,
            "timeout": int(cfg.get("statement_timeout_seconds", DEFAULT_STATEMENT_TIMEOUT_S)),
        }
        for key in ("database", "schema", "warehouse", "role"):
            if cfg.get(key):
                body[key] = cfg[key]
        if bindings:
            body["bindings"] = bindings
        body["parameters"] = {"MULTI_STATEMENT_COUNT": "1"}
        return body

    def submit(self, body: JSON) -> JSON:
        return self.statement_doc("POST", STATEMENTS, json=body)

    def complete(self, submitted: JSON) -> JSON:
        """Poll a submitted statement until it finishes (cancelled at the deadline); the final
        answer."""
        timeout = int(
            self._conn.config.get("statement_timeout_seconds", DEFAULT_STATEMENT_TIMEOUT_S)
        )
        done = sqlapi.run_statement(
            self._ctx,
            sqlapi.StatementCalls(
                submit=lambda: submitted,
                poll=lambda handle: self.statement_doc("GET", f"{STATEMENTS}/{handle}"),
                cancel=lambda handle: self.request("POST", f"{STATEMENTS}/{handle}/cancel").close(),
                statement_id=_handle,
                pending=lambda d: bool(d["pending"]),
            ),
            timeout_seconds=timeout,
        )
        doc: JSON = done["doc"]
        return doc

    def run(self, body: JSON) -> JSON:
        return self.complete(self.submit(body))

    # ------------------------------------------------------------------ invoke
    def invoke(self, request: JSON, *, write: bool = False) -> InvokeResult:
        try:
            return self._invoke(request, write)
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            raise AdapterFailure(ErrorClass.CONFIG, "malformed request") from exc

    def _invoke(self, request: JSON, write: bool) -> InvokeResult:
        statement = str(request.get("sql") or "")
        if not statement.strip():
            raise AdapterFailure(ErrorClass.CONFIG, "request needs `sql`")
        if not write:
            sqlapi.check_read_only(statement)
        params = request.get("params")
        if isinstance(params, dict):
            raise AdapterFailure(
                ErrorClass.CONFIG,
                "Snowflake takes positional parameters only: send params as an array",
            )
        bindings = {str(i): _binding(v) for i, v in enumerate(params or [], start=1)}
        max_rows = int(self._conn.config.get("max_rows", DEFAULT_MAX_ROWS))
        columns, rows, truncated = _rows(self.run(self.body(statement, bindings)), max_rows)
        return InvokeResult(
            {"columns": columns, "rows": rows, "row_count": len(rows)}, truncated=truncated
        )

    def close(self) -> None:
        self._client.close()


def _handle(d: JSON) -> str | None:
    handle = d["doc"].get("statementHandle")
    return handle if isinstance(handle, str) and _HANDLE.fullmatch(handle) else None


def _rows(doc: JSON, cap: int) -> tuple[list[str], list[Any], bool]:
    """(column names, at most `cap` rows of the first partition, truncated)."""
    meta = doc.get("resultSetMetaData") or {}
    columns = [str(c.get("name")) for c in meta.get("rowType") or [] if isinstance(c, dict)]
    data = doc.get("data") or []
    rows, capped = sqlapi.cap_rows(data, cap)
    total = meta.get("numRows")
    more = isinstance(total, int) and total > len(data)
    return columns, rows, capped or more or len(meta.get("partitionInfo") or []) > 1


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def columns_statement(database: str) -> str:
    # the only interpolated name is quoted as an identifier ('"' doubled); the cap is a constant
    return (
        "SELECT table_schema, table_name, column_name, data_type, is_nullable "  # nosec B608
        f"FROM {quote_identifier(database)}.INFORMATION_SCHEMA.COLUMNS "
        "WHERE table_schema <> 'INFORMATION_SCHEMA' ORDER BY 1, 2 "
        f"LIMIT {MAX_COLUMN_ROWS}"
    )


class SnowflakeAdapter:
    descriptor: ClassVar[AdapterDescriptor] = AdapterDescriptor(
        key="snowflake",
        kind="connection",
        version="1.0.0",
        display_name="Snowflake SQL",
        capabilities=frozenset({Capability.QUERY_SQL, Capability.DISCOVER_SCHEMA}),
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["account", "user"],
            "properties": {
                "account": {
                    "type": "string",
                    "pattern": ACCOUNT_PATTERN,
                    "title": "Account identifier",
                },
                "user": {"type": "string", "minLength": 1, "title": "User"},
                "role": {"type": "string", "title": "Role"},
                "warehouse": {"type": "string", "title": "Warehouse"},
                "database": {"type": "string", "title": "Default database"},
                "schema": {"type": "string", "title": "Default schema"},
                "databases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_DATABASES,
                    "title": "Databases to discover",
                },
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
                    "maximum": 120,
                    "default": DEFAULT_STATEMENT_TIMEOUT_S,
                    "title": "Statement timeout (seconds)",
                },
            },
        },
        secret_fields=("private_key", "token"),
        destination_fields=("account",),
        auth_methods=(KEY_PAIR, "bearer"),
        connection_type="data_platform",
        declared_maturity="experimental",
    )

    # ------------------------------------------------------------------ hooks (B-2753)
    def validate_credential(self, auth_method: str, value: str) -> list[ConfigError]:
        if auth_method == KEY_PAIR:
            key = _private_key(value)
            return [ConfigError("value", key)] if isinstance(key, str) else []
        if auth_method == "bearer" and not value.strip():
            return [ConfigError("value", EMPTY_TOKEN)]
        return []

    def validate_config(self, config: JSON) -> list[ConfigError]:
        return validate_against_schema(self.descriptor, config, extra=lambda _c: [])

    # ------------------------------------------------------------------ test / discover / open
    def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome:
        rec = CheckRecorder()
        errors = self.validate_config(conn.config)
        if errors:
            rec.fail("config", "; ".join(f"{e.path}: {e.message}" for e in errors)[:400])
            return ProbeOutcome(rec.finish(), ErrorClass.CONFIG, "configuration is invalid")
        try:
            host, _ = _stored_account(conn.config)
            session = SnowflakeSession(ctx, conn)
        except AdapterFailure as exc:
            rec.fail("config", exc.message)
            return ProbeOutcome(rec.finish(), exc.error_class, exc.message)
        rec.ok("config", "configuration is valid")
        try:
            if not standard_network_checks(ctx, host, rec):
                return ProbeOutcome(rec.finish(), ErrorClass.EGRESS, "destination denied by policy")
            return self._probe(session, rec)
        finally:
            session.close()

    def _probe(self, session: SnowflakeSession, rec: CheckRecorder) -> ProbeOutcome:
        try:
            submitted = session.submit(session.body(TEST_STATEMENT))
        except Exception as exc:  # noqa: BLE001 - classified below, never leaks the message
            failure = classify_exception(exc)
            if failure.error_class is ErrorClass.CONFIG:  # 404: authenticated, but not found
                rec.ok("tls", "certificate verified for the requested host name")
                rec.ok("auth", "credentials accepted")
                message = "the warehouse, database or role was not found (HTTP 404)"
                rec.fail("capability", message)
                return ProbeOutcome(rec.finish(), ErrorClass.CONFIG, message)
            name = {ErrorClass.TLS: "tls", ErrorClass.DNS: "dns"}.get(failure.error_class, "auth")
            if name == "auth" and isinstance(exc, AdapterFailure):
                rec.ok("tls", "certificate verified for the requested host name")
            rec.fail(name, failure.message)
            return ProbeOutcome(rec.finish(), failure.error_class, failure.message)
        rec.ok("tls", "certificate verified for the requested host name")
        rec.ok("auth", "credentials accepted")
        try:
            doc = session.complete(submitted)
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            failure = classify_exception(exc)
            rec.fail("capability", failure.message)
            return ProbeOutcome(rec.finish(), failure.error_class, failure.message)
        _, rows, _ = _rows(doc, 1)
        version = str(rows[0][3]) if rows and isinstance(rows[0], list) and len(rows[0]) > 3 else ""
        shown = version if _VERSION.fullmatch(version) else "unknown"
        rec.ok("capability", f"statement ran through the SQL API; Snowflake version {shown}")
        return ProbeOutcome(rec.finish())

    def discover(self, ctx: ExecContext, conn: ConnectionConfig) -> DiscoveryResult:
        session = SnowflakeSession(ctx, conn)
        try:
            return self._discover(session, conn)
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            raise classify_exception(exc) from exc
        finally:
            session.close()

    def _discover(self, session: SnowflakeSession, conn: ConnectionConfig) -> DiscoveryResult:
        truncated = False
        listed = conn.config.get("databases")
        if listed:
            databases = [str(d) for d in listed][:MAX_DATABASES]
        else:
            columns, rows, more = _rows(session.run(session.body(SHOW_DATABASES)), MAX_DATABASES)
            at = columns.index("name") if "name" in columns else 1
            databases = [str(r[at]) for r in rows if isinstance(r, list) and len(r) > at]
            truncated = more
        tables: list[JSON] = []
        for database in databases:
            doc = session.run(session.body(columns_statement(database)))
            _, rows, more = _rows(doc, MAX_COLUMN_ROWS)
            truncated = truncated or more or len(rows) >= MAX_COLUMN_ROWS
            tables.extend(_tables(database, rows))
        return DiscoveryResult("schema", {"tables": tables, "truncated": truncated})

    def open(self, ctx: ExecContext, conn: ConnectionConfig) -> SnowflakeSession:
        return SnowflakeSession(ctx, conn)


def _tables(database: str, rows: list[Any]) -> list[JSON]:  # Any: result rows
    """Rows of (schema, table, column, type, nullable) grouped into tables, in order."""
    tables: dict[tuple[str, str], JSON] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        schema, name, column, data_type, nullable = (str(v) for v in row[:5])
        table = tables.setdefault(
            (schema, name),
            {
                "catalog": database,
                "schema": schema,
                "name": name,
                "table_type": None,
                "columns": [],
            },
        )
        table["columns"].append(
            {"name": column, "type": data_type, "nullable": nullable.upper() == "YES"}
        )
    return list(tables.values())
