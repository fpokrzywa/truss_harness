"""Application settings (pydantic-settings, `SHIFDE_*` environment variables).

In development (``SHIFDE_ENV=development``) the repo-local ``.devdata/dev.env`` file is used as a
fallback source; real environment variables always win. Other environments never read it.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sys
from collections.abc import Callable, Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from shifde.core.crypto import KeyRing

# backend/src/shifde/core/settings.py -> parents[4] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[4]
DEV_ENV_FILE = REPO_ROOT / ".devdata" / "dev.env"
DEV_USERS_FILE = REPO_ROOT / ".devdata" / "dev-users.txt"

Environment = Literal["development", "test", "production"]

_EPHEMERAL: dict[str, str] = {}  # per-process generated dev/test values (never in production)


def _ephemeral(name: str, make: Callable[[], str]) -> str:
    if name not in _EPHEMERAL:
        _EPHEMERAL[name] = make()
    return _EPHEMERAL[name]


# Narrowest-acceptable trusted-proxy prefixes in production: anything broader (0.0.0.0/0,
# 0.0.0.0/1 + 128.0.0.0/1, ::/0, ...) trusts a large part of the Internet. 10.0.0.0/8 and the
# IPv6 unique-local range fc00::/7 are the widest legitimate private ranges.
_MIN_PROXY_PREFIX = {4: 8, 6: 7}
_PEPPER_ID = re.compile(r"[A-Za-z0-9_-]{1,16}")
_HOST_SUFFIX = re.compile(r"[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+")


def _new_pepper() -> str:
    return secrets.token_urlsafe(48)


# Settings that carry secret material. Each also accepts `SHIFDE_<NAME>_FILE`: the path of a file
# holding the value (mounted / OS-protected secret file). Setting both forms is an error.
SECRET_FIELDS: tuple[str, ...] = (
    "database_url",  # carries the application role password
    "migration_database_url",  # carries the owner role password
    "api_key_pepper",
    "api_key_retired_peppers",  # earlier peppers, still accepted for keys minted under them
    "secret_keys",  # the KEK ring
    "metrics_token",
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class SecretFileError(ValueError):
    """A `*_FILE` secret indirection is misconfigured (message never contains the secret)."""


def resolve_secret_files(environ: Mapping[str, str]) -> dict[str, str]:
    """Settings kwargs read from `SHIFDE_<NAME>_FILE` indirections in `environ`.

    An empty `_FILE` variable counts as unset. Both `SHIFDE_<NAME>` and `SHIFDE_<NAME>_FILE`
    set is an error, as are an unreadable or empty file. Only trailing line breaks (and a BOM)
    are stripped from the file content."""
    values: dict[str, str] = {}
    for name in SECRET_FIELDS:
        env_name = f"SHIFDE_{name.upper()}"
        file_name = f"{env_name}_FILE"
        path = environ.get(file_name)
        if not path:
            continue
        if environ.get(env_name):
            raise SecretFileError(f"{env_name} and {file_name} are both set; set only one")
        try:
            raw = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise SecretFileError(
                f"{file_name}: cannot read the secret file ({exc.__class__.__name__})"
            ) from None
        except UnicodeDecodeError:
            raise SecretFileError(f"{file_name}: the secret file is not valid UTF-8") from None
        value = raw.rstrip("\r\n")
        if not value:
            raise SecretFileError(f"{file_name}: the secret file is empty")
        values[name] = value
    return values


def _mask_url(url: str) -> str:
    """The URL with its password replaced by `***` (never raises)."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except ArgumentError:
        return "<unparseable url>"


def format_validation_error(exc: ValueError) -> str:
    """Redacted rendering of a settings error: variable name and message only. pydantic's own
    string form embeds `input_value=...` (for model validators the whole input, secrets
    included)."""
    if not isinstance(exc, ValidationError):
        return str(exc)
    lines: list[str] = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = err.get("loc", ())
        subject = f"SHIFDE_{str(loc[0]).upper()}" if loc else "configuration"
        msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
        lines.append(f"{subject}: {msg}")
    return "; ".join(lines) or "invalid configuration"


class MigrationUrlMissing(RuntimeError):
    """`SHIFDE_MIGRATION_DATABASE_URL` is required to run migrations and is not set."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SHIFDE_",
        extra="ignore",
        # pydantic's error text embeds `input_value=...` (for model validators the whole input,
        # i.e. the KEK ring and database passwords); never let that reach a log or a terminal.
        hide_input_in_errors=True,
        env_file_encoding="utf-8-sig",  # dev.env is written by PowerShell and may carry a BOM
    )

    env: Environment = "production"
    deployment_mode: Literal["isolated", "shared"] = "isolated"
    database_url: str
    migration_database_url: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = 8710
    # seconds uvicorn waits for open requests after a stop signal (SP4 design 6.2)
    api_shutdown_grace_seconds: int = Field(default=30, ge=1)
    auth_local_enabled: bool = False
    auth_local_allow_in_production: bool = False
    login_rate_per_minute: int = 20  # per client IP (skipped for loopback / trusted proxies)
    login_rate_per_account_per_minute: int = 10  # per normalized email
    # X-Forwarded-For is believed from these peers and from loopback (the same-host frontend proxy)
    trusted_proxies: Annotated[list[str], NoDecode] = []
    # Loopback peers may set the client address through X-Forwarded-For only when this is true.
    # Default false (B-2280): the same-host web proxy is not trusted until the operator lists it
    # (`SHIFDE_TRUSTED_PROXIES=127.0.0.1/32` or this flag), because a client reaching the API over
    # loopback by any route could otherwise choose the address the limiters and audit use.
    trust_loopback_proxy: bool = False
    max_request_body_bytes: int = 8 * 1024 * 1024  # global request-body cap, 0 = off (Item 5b)
    # Strict-Transport-Security max-age the API sends in production; 0 = off (SP4, B-2772)
    hsts_max_age_seconds: int = Field(default=31536000, ge=0)
    db_connect_timeout_seconds: int = 10  # Item 18
    db_statement_timeout_ms: int = 60_000  # Item 18; 0 = none
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    allowed_origins: Annotated[list[str], NoDecode] = [
        "http://127.0.0.1:3710",
        "http://localhost:3710",
    ]
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    # Secrets (SECURITY_MODEL 2.4 / 5). Required outside development/test; in development they
    # are generated once into .devdata/dev.env, in tests they are ephemeral per process.
    api_key_pepper: SecretStr | None = None
    # Pepper rotation (findings Item 4, B-916): new keys are hashed with `api_key_pepper` under
    # `api_key_pepper_id`; keys minted under an earlier pepper still verify while that pepper is
    # listed here as `<id>:<pepper>[,<id>:<pepper>...]`.
    api_key_pepper_id: str = "p1"
    api_key_retired_peppers: SecretStr | None = None
    api_key_failures_per_minute: int = 30  # failed bearer attempts per client address (Item 5b)
    executor_auth_failures_per_minute: int = 30  # failed executor tokens per address (B-2730)
    secret_keys: SecretStr | None = None
    api_key_max_days: int = 365
    # OIDC redirect base; required in production, elsewhere the default is the first allowed origin
    public_base_url: str | None = None
    oidc_state_ttl_seconds: int = 600
    oidc_jwks_min_refresh_seconds: int = 30
    oidc_jwks_cache_seconds: int = 3600  # JWKS lifetime when the IdP sends no usable max-age
    oidc_jwks_max_cache_seconds: int = 3600  # cap on any JWKS lifetime, max-age included
    oidc_http_timeout_seconds: float = 5.0
    # OIDC sign-in starts and callbacks per client address and minute (findings Item 5b)
    oidc_rate_per_minute: int = 20
    # Hard overall deadline of one governed model call; concurrency leases outlive it.
    model_call_deadline_seconds: float = 120.0
    # The SHI-wide model provider's endpoint and key (design 5.3, DECISIONS B-1712, B-1732):
    # installation configuration, never in a client-readable row. `SHIFDE_SHI_PROVIDER_CONFIG` is
    # the adapter's connection config as JSON (e.g. {"base_url": ...}); the key is sent with the
    # adapter's first non-"none" auth method. Read only on the call path of a shared SHI model.
    shi_provider_config: dict[str, Any] | None = None  # Any: adapter config JSON
    shi_provider_api_key: SecretStr | None = None
    # Hard overall deadline of one direct connection invoke (REST / MCP / SQL) through the API.
    connection_call_deadline_seconds: float = 60.0
    # Native delivery (design 3, B-2501): host suffixes this installation allows for Databricks
    # deployment targets in addition to the built-in cloud.databricks.com, azuredatabricks.net and
    # gcp.databricks.com. `SHIFDE_DELIVERY_DATABRICKS_HOST_SUFFIXES` is comma-separated; each entry
    # is a bare domain of two or more labels (no scheme, port, wildcard or leading dot).
    delivery_databricks_host_suffixes: Annotated[list[str], NoDecode] = []
    # Development switch (design 2026-11-22 7.5, B-2755): `databricks` and `snowflake` connections
    # reach the in-process fakes (`shifde.adapters.dataplatforms.fakes`) instead of the network,
    # for the local e2e and demo. Refused unless `env` is development or test.
    dev_fake_data_platforms: bool = False
    # P5 runtime
    worker_poll_ms: int = 1000
    worker_lease_seconds: int = 60
    worker_concurrency: int = 4
    queue_tenant_max_inflight: int = 0  # 0 = no per-tenant cap on leased jobs
    # abandoned (wedged) run threads a worker keeps before it stops claiming and exits 1 (to be
    # restarted), and per client before that client's claimed jobs are handed back (ARCH M3)
    worker_max_abandoned: int = Field(default=4, ge=1)
    worker_max_abandoned_per_client: int = Field(default=2, ge=1)
    # the worker's `/healthz` + `/readyz` port (0 = off) and how long a stop waits for running
    # jobs before leaving them to lease expiry and the reaper (SP4 design 6.2)
    worker_health_host: str = "127.0.0.1"
    worker_health_port: int = Field(default=0, ge=0, le=65535)
    worker_shutdown_grace_seconds: int = Field(default=30, ge=1)
    run_default_deadline_seconds: int = 3600
    approval_expiry_hours: int = 24
    runtime_output_reserve_tokens: int = 256  # budget hold for the completion side of a call
    runtime_retry_base_seconds: float = 1.0  # exponential backoff with full jitter (6.7)
    runtime_retry_cap_seconds: float = 60.0
    runtime_model_attempts: int = 3  # attempts per model before the next fallback model
    # P6: pipeline schedules are stored and listed only when enabled (cron execution is NOT part
    # of the MVP, OD-8)
    feature_pipeline_schedules: bool = False
    # P8 observability. Trace export: none (in-process ring only) | console | otlp (OTLP/HTTP JSON
    # to SHIFDE_OTEL_OTLP_ENDPOINT, falling back to OTEL_EXPORTER_OTLP_ENDPOINT). `/api/metrics`
    # needs `Authorization: Bearer <SHIFDE_METRICS_TOKEN>` when a token is set, else it answers
    # loopback clients only.
    otel_exporter: Literal["none", "console", "otlp"] = "none"
    otel_otlp_endpoint: str | None = None
    metrics_token: SecretStr | None = None  # required in production
    # `/api/docs` and `/api/openapi.json`; unset: off in production, on in development and test
    api_docs_enabled: bool | None = None

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().rstrip("/") for part in value.split(",") if part.strip()]
        return value

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:  # Any: pydantic's own signature
        """`repr()` and `str()` show the database URLs without their passwords (ARCH m1);
        `SecretStr` fields already mask themselves."""
        for name, value in super().__repr_args__():
            if name in ("database_url", "migration_database_url") and isinstance(value, str):
                yield name, _mask_url(value)
            else:
                yield name, value

    @field_validator("delivery_databricks_host_suffixes", mode="before")
    @classmethod
    def _split_host_suffixes(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value

    @field_validator("delivery_databricks_host_suffixes")
    @classmethod
    def _valid_host_suffixes(cls, value: list[str]) -> list[str]:
        suffixes = [entry.strip().lower() for entry in value]
        for entry in suffixes:
            if not _HOST_SUFFIX.fullmatch(entry):
                raise ValueError(
                    "SHIFDE_DELIVERY_DATABRICKS_HOST_SUFFIXES entries must be bare domains "
                    "of two or more letter, digit or hyphen labels"
                )
        return suffixes

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_proxies(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _valid_proxies(cls, value: list[str]) -> list[str]:
        for entry in value:
            ipaddress.ip_network(entry, strict=False)  # raises ValueError on garbage
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.dev_fake_data_platforms and self.env not in ("development", "test"):
            raise ValueError(
                "SHIFDE_DEV_FAKE_DATA_PLATFORMS is allowed only when SHIFDE_ENV is development "
                "or test"
            )
        if (
            self.env == "production"
            and self.auth_local_enabled
            and not self.auth_local_allow_in_production
        ):
            raise ValueError(
                "SHIFDE_AUTH_LOCAL_ENABLED=true is refused in production unless "
                "SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION=true is also set (NFR-SEC-001)"
            )
        if self.env == "production":
            if not self.metrics_token or not self.metrics_token.get_secret_value():
                raise ValueError("SHIFDE_METRICS_TOKEN is required in production")
            if self.log_format == "console":
                raise ValueError("SHIFDE_LOG_FORMAT=console is refused in production (use json)")
            if not self.public_base_url:
                raise ValueError("SHIFDE_PUBLIC_BASE_URL is required in production")
            for origin in self.allowed_origins:
                parts = urlsplit(origin)
                scheme, host = parts.scheme, parts.hostname or ""
                if scheme != "https" and host not in _LOOPBACK_HOSTS:
                    raise ValueError(
                        "SHIFDE_ALLOWED_ORIGINS must be https:// origins in production "
                        "(plain http is accepted for loopback only)"
                    )
            base = urlsplit(self.public_base_url)
            if base.scheme != "https" and (base.hostname or "") not in _LOOPBACK_HOSTS:
                raise ValueError(
                    "SHIFDE_PUBLIC_BASE_URL must be an https:// URL in production "
                    "(plain http is accepted for loopback only)"
                )
            for entry in self.trusted_proxies:
                net = ipaddress.ip_network(entry, strict=False)
                if net.prefixlen < _MIN_PROXY_PREFIX[net.version]:
                    raise ValueError(
                        f"SHIFDE_TRUSTED_PROXIES entry {entry!r} is too broad: it would trust a "
                        f"large share of the Internet (minimum /{_MIN_PROXY_PREFIX[net.version]} "
                        f"for IPv{net.version})"
                    )
        if self.api_docs_enabled is None:
            self.api_docs_enabled = self.env != "production"
        if self.session_idle_minutes < 1 or self.session_absolute_hours < 1:
            raise ValueError("session timeouts must be positive")
        generated = self.env in ("development", "test")
        if not self.api_key_pepper or not self.api_key_pepper.get_secret_value():
            if not generated:
                raise ValueError("SHIFDE_API_KEY_PEPPER is required outside development/test")
            self.api_key_pepper = SecretStr(_ephemeral("api_key_pepper", _new_pepper))
        elif len(self.api_key_pepper.get_secret_value()) < 32:
            raise ValueError("SHIFDE_API_KEY_PEPPER must be at least 32 characters")
        _ = self.pepper_ring  # parse now: fail fast on a malformed pepper ring
        if not self.secret_keys or not self.secret_keys.get_secret_value():
            if not generated:
                raise ValueError("SHIFDE_SECRET_KEYS is required outside development/test")
            self.secret_keys = SecretStr(_ephemeral("secret_keys", KeyRing.generate_entry))
        KeyRing.parse(self.secret_keys.get_secret_value())  # fail fast on a malformed keyring
        if not 1 <= self.api_key_max_days <= 3650:
            raise ValueError("SHIFDE_API_KEY_MAX_DAYS must be between 1 and 3650")
        return self

    @property
    def is_development(self) -> bool:
        return self.env == "development"

    @property
    def cookie_secure(self) -> bool:
        return not self.is_development

    @property
    def session_cookie_name(self) -> str:
        return "shifde_session" if self.is_development else "__Host-shifde_session"

    @property
    def oidc_cookie_name(self) -> str:
        return "shifde_oidc_bind" if self.is_development else "__Host-shifde_oidc_bind"

    @property
    def keyring(self) -> KeyRing:
        assert self.secret_keys is not None  # guaranteed by the validator
        return KeyRing.parse(self.secret_keys.get_secret_value())

    @property
    def pepper(self) -> bytes:
        assert self.api_key_pepper is not None  # guaranteed by the validator
        return self.api_key_pepper.get_secret_value().encode()

    @property
    def pepper_ring(self) -> dict[str, bytes]:
        """Pepper id -> pepper: the current one (`api_key_pepper_id`) and the retired ones."""
        if not _PEPPER_ID.fullmatch(self.api_key_pepper_id):
            raise ValueError("SHIFDE_API_KEY_PEPPER_ID must be 1-16 letters, digits, '-' or '_'")
        ring = {self.api_key_pepper_id: self.pepper}
        retired = self.api_key_retired_peppers
        raw = retired.get_secret_value() if retired else ""
        for part in (p.strip() for p in raw.split(",")):
            if not part:
                continue
            pid, sep, value = part.partition(":")
            if not sep or not _PEPPER_ID.fullmatch(pid) or pid in ring:
                raise ValueError(
                    "SHIFDE_API_KEY_RETIRED_PEPPERS entries are <id>:<pepper> with distinct ids, "
                    "none equal to SHIFDE_API_KEY_PEPPER_ID"
                )
            if len(value) < 32:
                raise ValueError("every retired API-key pepper must be at least 32 characters")
            ring[pid] = value.encode()
        return ring

    @property
    def pkce_key(self) -> bytes:
        """Key for OIDC PKCE verifiers (findings Item 4): derived from the active secret KEK with
        its own label, never from the API-key pepper (rotating one must not touch the other)."""
        ring = self.keyring
        return hmac.new(
            ring.kek(ring.active_id), b"shifde-oidc-pkce-key-v1", hashlib.sha256
        ).digest()

    @property
    def key_env_label(self) -> str:
        return {"development": "dev", "test": "test", "production": "prod"}[self.env]

    @property
    def oidc_base_url(self) -> str:
        """`SHIFDE_PUBLIC_BASE_URL` (always set in production), else the first allowed origin."""
        return (self.public_base_url or self.allowed_origins[0]).rstrip("/")

    @property
    def effective_migration_url(self) -> str:
        """The schema owner's URL. Unlike SETH there is no fallback to `database_url`: migrations
        never run as the application role (isolation review 1, item 13)."""
        if not self.migration_database_url:
            raise MigrationUrlMissing(
                "SHIFDE_MIGRATION_DATABASE_URL is not set; migrations run only as the schema owner"
            )
        return self.migration_database_url


def _ensure_dev_env_values(path: Path) -> None:
    """Development only: generate the API-key pepper / secret KEK into dev.env once, so keys and
    secrets created in dev survive restarts. Real environment variables still win."""
    text = path.read_text(encoding="utf-8-sig")
    present = {line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line}
    additions: dict[str, Callable[[], str]] = {
        "SHIFDE_API_KEY_PEPPER": _new_pepper,
        "SHIFDE_SECRET_KEYS": KeyRing.generate_entry,
    }
    missing = [
        f"{k}={make()}"
        for k, make in additions.items()
        if k not in present and k not in os.environ and f"{k}_FILE" not in os.environ
    ]
    if missing:
        sep = "" if text.endswith("\n") or not text else "\n"
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(sep + "# Generated by shifde (development only). Do not commit.\n")
            fh.write("\n".join(missing) + "\n")


def load_settings() -> Settings:
    """Build settings; load `.devdata/dev.env` only when SHIFDE_ENV=development. `SHIFDE_*_FILE`
    secret indirections are resolved here (see `resolve_secret_files`)."""
    env_file: Path | None = None
    environ: dict[str, str] = dict(os.environ)
    if os.environ.get("SHIFDE_ENV", "").strip().lower() == "development" and DEV_ENV_FILE.exists():
        env_file = DEV_ENV_FILE
        _ensure_dev_env_values(env_file)
        environ = {
            **{k: v for k, v in dotenv_values(env_file, encoding="utf-8-sig").items() if v},
            **environ,
        }
    file_values = resolve_secret_files(environ)
    return Settings(_env_file=env_file, **file_values)  # type: ignore[arg-type]


def settings_or_exit() -> Settings:
    """`get_settings()` for process entrypoints (api, worker): a configuration error prints one
    redacted line to stderr and exits 2 instead of escaping as a traceback."""
    try:
        return get_settings()
    except ValueError as exc:  # pydantic ValidationError / SecretFileError subclass ValueError
        # `SystemExit(<str>)` would exit 1; configuration errors exit 2 (SP4 design 4)
        print(f"shifde: invalid configuration: {format_validation_error(exc)}", file=sys.stderr)
        raise SystemExit(2) from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
