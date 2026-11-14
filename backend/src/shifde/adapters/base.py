"""Adapter contracts (SYSTEM_ARCHITECTURE 10): descriptors, capabilities, protocols, value objects.

Adapters never read the environment or the database: they receive configuration, a resolved
`Credential` and an `ExecContext` whose `egress` guard is the only sanctioned way to touch the
network. `AdapterDescriptor` is declarative; *maturity* is not set here beyond the ceiling an
adapter can claim (`planned` or `experimental`): `validated` is derived from recorded conformance
evidence (see `shifde.adapters.maturity`) and can never be declared in code or by an admin.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol  # Any: JSON documents / JSON Schema
from uuid import UUID

from jsonschema import Draft202012Validator

from shifde.core.egress import EgressGuard
from shifde.core.redaction import register_secret

JSON = dict[str, Any]  # Any: free-form JSON object (config, results, schemas)

CONNECTION_TYPES = ("rest", "mcp", "database", "data_platform", "model_api")
AUTH_METHODS = (
    "none",
    "api_key",
    "bearer",
    "oauth2_client_credentials",
    "basic",
    "key_pair",
    "mtls",
)


class Capability(StrEnum):
    MODEL_CHAT = "model.chat"
    MODEL_LIST = "model.list"
    MODEL_HEALTH = "model.health"
    MODEL_STREAM = "model.stream"
    MODEL_TOOLS = "model.tools"
    QUERY_SQL = "query.sql"
    QUERY_PUSHDOWN_JOIN = "query.pushdown_join"
    DISCOVER_SCHEMA = "discover.schema"
    DISCOVER_OPENAPI = "discover.openapi"
    MCP_TOOLS = "mcp.tools"
    REST_CALL = "rest.call"


@dataclass(frozen=True)
class AdapterDescriptor:
    key: str
    kind: Literal["model_provider", "connection", "vendor", "tool_executor", "secret_provider"]
    version: str
    display_name: str
    capabilities: frozenset[Capability]
    config_schema: JSON
    secret_fields: tuple[str, ...] = ()
    # Config properties that decide WHERE a bound secret is sent (host, URL, port, TLS mode). A
    # change to one needs `secret:use` on the bound secret (FR-CON-011); see ConnectionService.
    destination_fields: tuple[str, ...] = ()
    auth_methods: tuple[str, ...] = ("none",)
    connection_type: str | None = None  # rest|mcp|database|data_platform|model_api (B5)
    # Ceiling the code may claim. `validated` is intentionally not representable here.
    declared_maturity: Literal["planned", "experimental"] = "experimental"

    @property
    def implemented(self) -> bool:
        return self.declared_maturity != "planned"


class ErrorClass(StrEnum):
    CONFIG = "config"
    EGRESS = "egress"
    DNS = "dns"
    TLS = "tls"
    AUTH = "auth"
    CAPABILITY = "capability"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    UPSTREAM = "upstream"
    NETWORK = "network"


class AdapterFailure(Exception):
    """A classified, already-redacted adapter failure (never contains credentials)."""

    def __init__(self, error_class: ErrorClass, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message


class Credential:
    """A resolved secret value; repr/str never reveal it. Registered with the redaction registry
    so a stray echo of it in logs / problem details / audit is masked."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value
        register_secret(value)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Credential(***)"

    __str__ = __repr__


@dataclass(frozen=True)
class ExecContext:
    tenant_id: UUID | None
    principal_id: UUID | None
    egress: EgressGuard
    request_id: str | None = None
    run_id: UUID | None = None
    step_id: UUID | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ConnectionConfig:
    id: UUID | None
    name: str
    adapter_key: str
    config: JSON
    auth_method: str = "none"
    access_mode: str = "read"
    credential: Credential | None = None


@dataclass(frozen=True)
class ConfigError:
    path: str
    message: str


@dataclass
class CheckResult:
    name: str  # config | egress | dns | tls | auth | capability
    status: Literal["pass", "fail", "skipped"]
    detail: str = ""


@dataclass
class ProbeOutcome:
    checks: list[CheckResult]
    error_class: ErrorClass | None = None
    message: str | None = None

    @property
    def passed(self) -> bool:
        return all(c.status != "fail" for c in self.checks) and any(
            c.status == "pass" for c in self.checks
        )


@dataclass
class DiscoveryResult:
    kind: Literal["schema", "mcp_tools", "openapi"]
    payload: JSON


@dataclass
class InvokeResult:
    result: Any  # Any: adapter-specific JSON-serialisable payload
    truncated: bool = False


class ConnectionSession(Protocol):
    """An opened, short-lived adapter session (`ConnectionService.open_for_use`)."""

    def invoke(self, request: JSON, *, write: bool = False) -> InvokeResult: ...

    def close(self) -> None: ...


class ConnectionAdapter(Protocol):
    descriptor: ClassVar[AdapterDescriptor]

    def validate_config(self, config: JSON) -> list[ConfigError]: ...

    def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome: ...

    def discover(self, ctx: ExecContext, conn: ConnectionConfig) -> DiscoveryResult: ...

    def open(self, ctx: ExecContext, conn: ConnectionConfig) -> ConnectionSession: ...


# ----------------------------------------------------------------------------- data platform
@dataclass(frozen=True)
class Filter:
    column: str
    op: Literal["=", "!=", "<", "<=", ">", ">=", "like", "in", "is_null"]
    value: Any = None  # Any: bound parameter value


@dataclass(frozen=True)
class FilterGroup:
    """Boolean structure over filters: `and` / `or` take one or more items, `not` exactly one."""

    op: Literal["and", "or", "not"]
    items: Sequence[FilterNode]


FilterNode = Filter | FilterGroup


@dataclass(frozen=True)
class SourceQueryPlan:
    table: str
    schema: str | None = None
    columns: Sequence[str] = ()
    filters: Sequence[FilterNode] = ()  # AND-combined at the top level
    order_by: Sequence[tuple[str, Literal["asc", "desc"]]] = ()
    limit: int | None = None
    offset: int | None = None


@dataclass(frozen=True)
class CompiledQuery:
    sql: str  # identifiers quoted, values as placeholders only
    params: tuple[Any, ...]  # Any: bound parameter values


class DataPlatformAdapter(ConnectionAdapter, Protocol):
    def compile(self, plan: SourceQueryPlan) -> CompiledQuery: ...

    def execute(
        self, ctx: ExecContext, conn: ConnectionConfig, q: CompiledQuery, *, max_rows: int
    ) -> InvokeResult: ...


# ----------------------------------------------------------------------------- model providers
@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str


@dataclass(frozen=True)
class ChatRequest:
    messages: Sequence[ChatMessage]
    max_tokens: int | None = None
    temperature: float | None = None


@dataclass(frozen=True)
class Usage:
    """Token counts as the provider reported them. `None` means the provider did not say: it is
    never turned into 0, so a caller can tell "unreported" (to be estimated and labelled so) from
    a real zero. `cached_input_tokens` is the provider's own cached-prompt figure, unadjusted
    (OpenAI counts it inside `input_tokens`; Anthropic reports it separately)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None

    @property
    def reported(self) -> bool:
        """True when both the input and the output count came from the provider."""
        return self.input_tokens is not None and self.output_tokens is not None


@dataclass(frozen=True)
class ChatResponse:
    output: str
    usage: Usage
    finish_reason: str | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True)
class ModelInfo:
    model_key: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class HealthResult:
    status: Literal["healthy", "degraded", "unavailable"]
    latency_ms: int
    detail: str = ""


class ModelProvider(ConnectionAdapter, Protocol):
    def generate(
        self, ctx: ExecContext, conn: ConnectionConfig, model_key: str, request: ChatRequest
    ) -> ChatResponse: ...

    def list_models(self, ctx: ExecContext, conn: ConnectionConfig) -> list[ModelInfo]: ...

    def health(self, ctx: ExecContext, conn: ConnectionConfig) -> HealthResult: ...

    def classify_error(self, exc: Exception) -> ErrorClass: ...


# ----------------------------------------------------------------------------- shared helpers
SENSITIVE_CONFIG_KEY = ("password", "secret", "token", "api_key", "apikey", "authorization")


def validate_against_schema(
    descriptor: AdapterDescriptor, config: JSON, *, extra: Callable[[JSON], list[ConfigError]]
) -> list[ConfigError]:
    """JSON Schema validation + the rule that secret fields never live in `config`."""
    errors: list[ConfigError] = []
    validator = Draft202012Validator(descriptor.config_schema)
    for err in sorted(validator.iter_errors(config), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(ConfigError(path, err.message[:300]))

    def walk(node: Any, prefix: str) -> None:  # Any: arbitrary JSON subtree
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower().replace("-", "_")
                if lowered in descriptor.secret_fields or any(
                    s in lowered for s in SENSITIVE_CONFIG_KEY
                ):
                    errors.append(
                        ConfigError(
                            f"{prefix}{key}",
                            "credentials must be supplied through a secret, not in config",
                        )
                    )
                else:
                    walk(value, f"{prefix}{key}/")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{prefix}{i}/")

    walk(config, "")
    if not errors:
        errors.extend(extra(config))
    return errors


@dataclass
class CheckRecorder:
    """Builds the ordered `checks` list: after the first failure the rest are `skipped`."""

    names: Sequence[str] = ("config", "egress", "dns", "tls", "auth", "capability")
    results: dict[str, CheckResult] = field(default_factory=dict)
    failed: bool = False

    def ok(self, name: str, detail: str = "") -> None:
        self.results[name] = CheckResult(name, "pass", detail)

    def skip(self, name: str, detail: str = "not applicable") -> None:
        self.results[name] = CheckResult(name, "skipped", detail)

    def fail(self, name: str, detail: str) -> None:
        self.results[name] = CheckResult(name, "fail", detail)
        self.failed = True

    def finish(self) -> list[CheckResult]:
        ordered: list[CheckResult] = []
        for name in self.names:
            ordered.append(
                self.results.get(
                    name,
                    CheckResult(
                        name, "skipped", "not reached" if self.failed else "not applicable"
                    ),
                )
            )
        return ordered
