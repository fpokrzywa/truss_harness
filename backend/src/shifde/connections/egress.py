"""Egress policies: administration (tenant + platform default) and effective-policy construction.

Effective policy = installation default AND tenant default AND the connection's policy; every set
layer must permit a request (most restrictive wins; see `shifde.core.egress`). A tenant or
connection policy can therefore only narrow what the installation default allows.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any  # Any: policy rows / validated rule values
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.dataplatforms import fakes as data_platform_fakes
from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.connections import repository
from shifde.connections import schemas as s
from shifde.core import db
from shifde.core.context import Principal, RequestContext
from shifde.core.egress import (
    SUPPORTED_SCHEMES,
    EgressGuard,
    EgressPolicy,
    installation_layer,
    parse_cidrs,
)
from shifde.core.errors import Conflict, NotFound, ScopeArchived, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.tenancy.scope import TenantScope

_HOST = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9._:-]*[A-Za-z0-9])?$")


def policy_from_row(row: Mapping[str, Any]) -> EgressPolicy:
    return EgressPolicy(
        allowed_schemes=frozenset(row["allowed_schemes"] or ()),
        allowed_ports=frozenset(row["allowed_ports"] or ()),
        allowed_hosts=tuple(row["allowed_hosts"] or ()),
        allowed_cidrs=parse_cidrs(row["allowed_cidrs"] or ()),
        allow_private=bool(row["allow_private"]),
        allow_redirects=bool(row["allow_redirects"]),
        max_response_bytes=int(row["max_response_bytes"]),
        timeout_seconds=float(row["timeout_seconds"]),
    )


def policy_out(row: Mapping[str, Any]) -> s.EgressPolicyOut:
    return s.EgressPolicyOut(
        id=row["id"],
        name=row["name"],
        is_default=row["is_default"],
        allowed_schemes=list(row["allowed_schemes"]),
        allowed_ports=list(row["allowed_ports"]),
        allowed_hosts=list(row["allowed_hosts"]),
        allowed_cidrs=list(row["allowed_cidrs"]),
        allow_private=row["allow_private"],
        allow_redirects=row["allow_redirects"],
        max_response_bytes=row["max_response_bytes"],
        timeout_seconds=row["timeout_seconds"],
        row_version=row["row_version"],
    )


def _rule_values(rules: s.EgressRules) -> dict[str, Any]:
    """Validated, normalised column values for the rule fields that were supplied."""
    values: dict[str, Any] = {}
    sent = rules.model_fields_set
    if "allowed_schemes" in sent and rules.allowed_schemes is not None:
        schemes = [x.lower() for x in rules.allowed_schemes]
        bad = [x for x in schemes if x not in SUPPORTED_SCHEMES]
        if bad:
            raise ValidationFailed(f"unsupported scheme(s): {', '.join(bad)}")
        values["allowed_schemes"] = sorted(set(schemes))
    if "allowed_ports" in sent and rules.allowed_ports is not None:
        if any(not 1 <= p <= 65535 for p in rules.allowed_ports):
            raise ValidationFailed("ports must be between 1 and 65535")
        values["allowed_ports"] = sorted(set(rules.allowed_ports))
    if "allowed_hosts" in sent and rules.allowed_hosts is not None:
        if any(not _HOST.match(h) or len(h) > 253 for h in rules.allowed_hosts):
            raise ValidationFailed("allowed_hosts entries must be host names, IPs or *.suffix")
        values["allowed_hosts"] = [h.lower() for h in rules.allowed_hosts]
    if "allowed_cidrs" in sent and rules.allowed_cidrs is not None:
        try:
            values["allowed_cidrs"] = [
                str(ipaddress.ip_network(c, strict=False)) for c in rules.allowed_cidrs
            ]
        except ValueError as exc:
            raise ValidationFailed("allowed_cidrs entries must be valid CIDR networks") from exc
    for key in ("allow_private", "allow_redirects", "max_response_bytes", "timeout_seconds"):
        if key in sent and getattr(rules, key) is not None:
            values[key] = getattr(rules, key)
    return values


# ------------------------------------------------------------------ effective guard
FAKE_DATA_PLATFORMS = frozenset({"databricks", "snowflake"})  # the dev switch's adapter keys


def build_guard(
    session: Session,
    runtime: IntegrationRuntime,
    tenant_id: UUID,
    connection_policy_id: UUID | None,
    *,
    adapter_key: str | None = None,
) -> EgressGuard:
    """The effective guard of a connection of `tenant_id`. The session must be in that client's
    context (connections review 1 item 1): under another or no client, row-level security would
    hide the client's own default policy and silently leave it out, so this refuses instead.

    With `SHIFDE_DEV_FAKE_DATA_PLATFORMS` (development/test only, B-2755) a `databricks` or
    `snowflake` connection gets the in-process fakes' transport and a loopback resolver; the
    policy layers are the same."""
    current = repository.context_tenant_id(session)
    if current is None or current != tenant_id:
        raise RuntimeError("an egress guard is built only in its own client's context")
    installation = repository.get_default_policy(session, None)
    layers = [
        installation_layer(
            policy_from_row(installation) if installation else None, env=runtime.settings.env
        )
    ]
    tenant_default = repository.get_default_policy(session, tenant_id)
    if tenant_default:
        layers.append(policy_from_row(tenant_default))
    if connection_policy_id is not None:
        row = repository.get_egress_policy(session, connection_policy_id, tenant_id=tenant_id)
        if row is not None and row["tenant_id"] == tenant_id:
            layers.append(policy_from_row(row))
        else:  # a dangling reference must never widen anything: deny everything
            layers.append(EgressPolicy(allowed_schemes=frozenset({"__none__"})))
    if runtime.settings.dev_fake_data_platforms and adapter_key in FAKE_DATA_PLATFORMS:
        return EgressGuard(
            layers,
            resolver=lambda host: ["127.0.0.1"],
            inner=data_platform_fakes.dev_transport(),
        )
    return EgressGuard(
        layers, resolver=runtime.resolver, inner=runtime.inner, inner_async=runtime.inner_async
    )


# ------------------------------------------------------------------ tenant policies
class EgressPolicyService:
    def __init__(self, scope: TenantScope) -> None:
        self._sc = scope

    def _read(self) -> None:
        # Policies are not secret and the connection wizard needs to offer them.
        if not self._sc.pdp.authorize("egress:manage", self._sc.tenant_ref).allowed:
            self._sc.pdp.require("connection:read", self._sc.tenant_ref)

    def _manage(self) -> None:
        self._sc.pdp.require("egress:manage", self._sc.tenant_ref)
        if self._sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")

    def _get(self, policy_id: UUID) -> Mapping[str, Any]:
        row = repository.get_egress_policy(
            self._sc.session, policy_id, tenant_id=self._sc.tenant_id
        )
        if row is None or row["tenant_id"] != self._sc.tenant_id:
            raise NotFound("egress policy not found")
        return row

    def _audit(self, action: str, row: Mapping[str, Any], details: dict[str, object]) -> None:
        audit.record(
            self._sc.session,
            self._sc.ctx,
            action=action,
            tenant_id=self._sc.tenant_id,
            target_type="egress_policy",
            target_id=row["id"],
            target_label=f"egress:{row['name']}",
            details=details,
        )

    def list(self, limit: int, cursor: str | None) -> Page[s.EgressPolicyOut]:
        self._read()
        rows = repository.list_egress_policies(
            self._sc.session, self._sc.tenant_id, decode_cursor(cursor), limit + 1
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[policy_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, policy_id: UUID) -> s.EgressPolicyOut:
        self._read()
        return policy_out(self._get(policy_id))

    def create(self, body: s.EgressPolicyCreate) -> s.EgressPolicyOut:
        self._manage()
        session = self._sc.session
        policy_id = uuid7()
        values: dict[str, Any] = {
            "allowed_schemes": ["https"],
            "allowed_ports": [443],
            "allowed_hosts": [],
            "allowed_cidrs": [],
            "allow_private": False,
            "allow_redirects": False,
            "max_response_bytes": 5_000_000,
            "timeout_seconds": 10.0,
            **_rule_values(body),
        }
        try:
            if body.is_default:
                self._demote_default()
            repository.insert_egress_policy(
                session,
                {
                    "id": policy_id,
                    "tenant_id": self._sc.tenant_id,
                    "name": body.name,
                    "is_default": body.is_default,
                    "created_by": self._sc.principal.principal_id,
                    **values,
                },
            )
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise Conflict("an egress policy with this name already exists") from exc
        row = self._get(policy_id)
        self._audit("egress.policy.created", row, {"rules": _rules_summary(row)})
        session.commit()
        return policy_out(row)

    def _demote_default(self) -> None:
        current = repository.get_default_policy(self._sc.session, self._sc.tenant_id)
        if current is not None:
            repository.update_egress_policy(
                self._sc.session,
                current["id"],
                None,
                {"is_default": False},
                tenant_id=self._sc.tenant_id,
            )

    def patch(self, policy_id: UUID, body: s.EgressPolicyPatch) -> s.EgressPolicyOut:
        self._manage()
        current = self._get(policy_id)
        values = _rule_values(body)
        if body.name is not None:
            values["name"] = body.name
        if body.is_default is not None:
            values["is_default"] = body.is_default
        if not values:
            raise ValidationFailed("no updatable fields supplied")
        session = self._sc.session
        try:
            if values.get("is_default") and not current["is_default"]:
                self._demote_default()
            updated = repository.update_egress_policy(
                session, policy_id, body.row_version, values, tenant_id=self._sc.tenant_id
            )
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise Conflict("an egress policy with this name already exists") from exc
        if updated is None:
            raise Conflict("row_version mismatch: the policy was modified; reload and retry")
        self._audit(
            "egress.policy.updated",
            updated,
            {"changed": sorted(values), "rules": _rules_summary(updated)},
        )
        session.commit()
        return policy_out(updated)


def _rules_summary(row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "schemes": list(row["allowed_schemes"]),
        "ports": list(row["allowed_ports"]),
        "hosts": len(row["allowed_hosts"]),
        "cidrs": list(row["allowed_cidrs"]),
        "allow_private": row["allow_private"],
        "allow_redirects": row["allow_redirects"],
    }


# ------------------------------------------------------------------ installation default
def get_platform_default(
    session: Session, ctx: RequestContext, principal: Principal
) -> s.EgressPolicyOut:
    authz.new_pdp(session, principal, None).require("platform:admin")
    row = repository.get_default_policy(session, None)
    if row is None:  # pragma: no cover - seeded by the migration
        raise NotFound("installation egress default not found")
    return policy_out(row)


def put_platform_default(
    session: Session, ctx: RequestContext, principal: Principal, body: s.EgressRules
) -> s.EgressPolicyOut:
    authz.new_pdp(session, principal, None).require("platform:admin")
    current = repository.get_default_policy(session, None)
    if current is None:  # pragma: no cover - seeded by the migration
        raise NotFound("installation egress default not found")
    values = _rule_values(body)
    if not values:
        raise ValidationFailed("no rule fields supplied")
    with db.platform_admin_context(session):
        updated = repository.update_egress_policy(
            session, current["id"], body.row_version, values, tenant_id=None
        )
        if updated is None:
            raise Conflict("row_version mismatch: the default was modified; reload and retry")
        audit.record(
            session,
            ctx,
            action="egress.default.updated",
            tenant_id=None,
            target_type="egress_policy",
            target_id=updated["id"],
            target_label="egress:installation-default",
            details={"changed": sorted(values), "rules": _rules_summary(updated)},
        )
        session.commit()
    return policy_out(updated)
