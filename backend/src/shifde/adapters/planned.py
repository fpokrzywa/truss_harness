"""Declared-only adapters (Microsoft Fabric): `planned`, no implementation.

They exist so a connection can be configured and shown honestly as "Declared - not validated";
`test`, `discover`, `open` and `invoke` answer 422 `adapter_not_implemented`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from shifde.adapters.base import (
    AdapterDescriptor,
    Capability,
    ConfigError,
    ConnectionConfig,
    DiscoveryResult,
    ExecContext,
    ProbeOutcome,
    validate_against_schema,
)
from shifde.core.errors import AdapterNotImplemented

_PLANNED_CAPS = frozenset(
    {Capability.QUERY_SQL, Capability.QUERY_PUSHDOWN_JOIN, Capability.DISCOVER_SCHEMA}
)


def _descriptor(key: str, name: str, required: list[str]) -> AdapterDescriptor:
    # the required property of every planned adapter names its target (host / account / endpoint)
    return AdapterDescriptor(
        key=key,
        kind="connection",
        version="0.0.0",
        display_name=name,
        capabilities=_PLANNED_CAPS,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": True,
            "required": required,
            "properties": {r: {"type": "string"} for r in required},
        },
        secret_fields=("token", "password", "private_key"),
        destination_fields=tuple(required),
        auth_methods=("none", "bearer", "key_pair", "oauth2_client_credentials"),
        connection_type="data_platform",
        declared_maturity="planned",
    )


class _PlannedAdapter:
    descriptor: ClassVar[AdapterDescriptor]

    def validate_config(self, config: dict[str, Any]) -> list[ConfigError]:
        return validate_against_schema(self.descriptor, config, extra=lambda _c: [])

    def _refuse(self) -> AdapterNotImplemented:
        return AdapterNotImplemented(
            f"adapter {self.descriptor.key!r} is declared but not implemented (planned)"
        )

    def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome:
        raise self._refuse()

    def discover(self, ctx: ExecContext, conn: ConnectionConfig) -> DiscoveryResult:
        raise self._refuse()

    def open(self, ctx: ExecContext, conn: ConnectionConfig) -> Any:
        raise self._refuse()


class FabricPlanned(_PlannedAdapter):
    descriptor = _descriptor("fabric", "Microsoft Fabric (planned)", ["endpoint"])
