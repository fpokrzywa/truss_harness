"""Model registry: providers and models of a client, and the SHI-wide provider with its per-client
switch.

Ported from SETH 3ba0e06 `models_admin/service.py` lines 1-552 (providers, health check,
discovery, models); allowlists, routes, effective models and governed invoke follow in part 2b.
Changes (DECISIONS B-1711..B-1719):

* Every row is read and written for the caller's resolved client only (`TenantScope.tenant_id`,
  from `tenancy.dependencies.resolve_org`): the repository keys each call by `tenant_id` as well
  as row-level security (B-1704). Another client's provider or model is answered like an unknown
  id.
* The shared SHI provider and its models (tenant NULL) are listed and readable by a client only
  while its switch is on; otherwise they are answered like unknown ids (B-1715). A client never
  writes them: a write naming one is refused (403) while they are visible, 404 otherwise.
* The SHI provider itself and the per-client switch are the platform administrator's
  (`platform_*` functions below, design 3 and 5.3): every switch change is audited in that client
  (its owner sees it) and at installation level (B-1717).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any  # Any: rows / provider JSON
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.base import AdapterFailure
from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.connections.service import ConnectionService, failure_to_error
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.errors import (
    Conflict,
    Forbidden,
    NotFound,
    ScopeArchived,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.core.redaction import scrub_text
from shifde.models_admin import repository
from shifde.models_admin import schemas as s
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope
from shifde.vendors import service as vendors

LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    "preview": frozenset({"active", "deprecated", "retired"}),
    "active": frozenset({"deprecated", "retired"}),
    "deprecated": frozenset({"active", "retired"}),
    "retired": frozenset(),
}
INVOCABLE = frozenset({"preview", "active"})

ORG_NOT_FOUND = "organization not found"  # the answer `tenancy.dependencies.resolve_org` gives
SHARED_READ_ONLY = "the SHI provider and its models are managed by the platform administrator"
SHI_NOT_CONFIGURED = "the SHI provider is not configured"
SHI_CONFIGURED = "model.shi_provider.configured"
SHI_ACCESS_CHANGED = "model.shi_provider.access_changed"


def _provider_out(r: Mapping[str, Any]) -> s.ProviderOut:
    return s.ProviderOut(
        id=r["id"],
        name=r["name"],
        adapter_key=r["adapter_key"],
        connection_id=r["connection_id"],
        vendor_id=r["vendor_id"],
        # the shared row has no settings (ck_model_providers__shared_client_safe, B-1712)
        settings=(r["settings"] or {}) if r["tenant_id"] is not None else {},
        status=r["status"],
        health_status=r["health_status"],
        health_checked_at=r["health_checked_at"],
        row_version=r["row_version"],
        created_at=r["created_at"],
    )


def _model_out(r: Mapping[str, Any]) -> s.ModelOut:
    return s.ModelOut(
        id=r["id"],
        provider_id=r["provider_id"],
        model_key=r["model_key"],
        display_name=r["display_name"],
        family=r["family"],
        version_label=r["version_label"],
        capabilities=list(r["capabilities"] or []),
        context_window=r["context_window"],
        max_output_tokens=r["max_output_tokens"],
        lifecycle=r["lifecycle"],
        deprecation_date=r["deprecation_date"],
        rate_limits=r["rate_limits"] or {},
        pricing_ref=r["pricing_ref"],
        row_version=r["row_version"],
        created_at=r["created_at"],
    )


def _access_out(r: Mapping[str, Any] | None) -> s.ShiAccessOut:
    if r is None:  # no row: off (B-1701)
        return s.ShiAccessOut(enabled=False, changed_at=None, row_version=None)
    return s.ShiAccessOut(
        enabled=r["enabled"], changed_at=r["changed_at"], row_version=r["row_version"]
    )


class ModelService:
    def __init__(self, scope: TenantScope, runtime: IntegrationRuntime) -> None:
        self.sc = scope
        self.runtime = runtime
        self.conns = ConnectionService(scope, runtime)

    # ================================================================== helpers
    @property
    def _session(self) -> Session:
        return self.sc.session

    def _manage(self, ref: ResourceRef | None = None) -> None:
        self.sc.pdp.require("model:manage", ref or self.sc.tenant_ref)
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")

    def _read(self, ref: ResourceRef | None = None) -> None:
        self.sc.pdp.require("model:read", ref or self.sc.tenant_ref)

    def _commit(self, what: str) -> None:
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise Conflict(f"{what} conflicts with an existing resource") from exc

    def _audit(
        self,
        action: str,
        target_type: str,
        target_id: UUID,
        label: str,
        details: dict[str, object],
        outcome: str = "success",
    ) -> None:
        audit.record(
            self._session,
            self.sc.ctx,
            action=action,
            outcome=outcome,
            tenant_id=self.sc.tenant_id,
            target_type=target_type,
            target_id=target_id,
            target_label=label,
            details=details,
        )

    def _shared(self) -> bool:
        """Whether this client sees the shared SHI provider and its models: its switch is on."""
        return repository.shi_enabled(self._session, tenant_id=self.sc.tenant_id)

    def _owned(self, row: Mapping[str, Any] | None, what: str, *, write: bool) -> Mapping[str, Any]:
        if row is None:
            raise NotFound(f"{what} not found")
        if row["tenant_id"] is None:
            if write:
                raise Forbidden(SHARED_READ_ONLY)
            return row
        if row["tenant_id"] != self.sc.tenant_id:
            raise NotFound(f"{what} not found")
        return row

    def _provider_row(self, provider_id: UUID, *, write: bool = False) -> Mapping[str, Any]:
        row = repository.get_provider(
            self._session, provider_id, tenant_id=self.sc.tenant_id, shared=self._shared()
        )
        return self._owned(row, "model provider", write=write)

    def _model_row(self, model_id: UUID, *, write: bool = False) -> Mapping[str, Any]:
        row = repository.get_model(
            self._session, model_id, tenant_id=self.sc.tenant_id, shared=self._shared()
        )
        return self._owned(row, "model", write=write)

    # ================================================================== providers
    def list_providers(self, limit: int, cursor: str | None) -> Page[s.ProviderOut]:
        self._read()
        rows = repository.list_providers(
            self._session,
            decode_cursor(cursor),
            limit + 1,
            tenant_id=self.sc.tenant_id,
            shared=self._shared(),
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[_provider_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get_provider(self, provider_id: UUID) -> s.ProviderOut:
        self._read()
        return _provider_out(self._provider_row(provider_id))

    def create_provider(self, body: s.ProviderCreate) -> s.ProviderOut:
        self._manage()
        desc = self.runtime.registry.descriptor(body.adapter_key)
        if desc is None or desc.kind != "model_provider":
            raise ValidationFailed(f"{body.adapter_key!r} is not a model provider adapter")
        conn = self.conns.get_row(body.connection_id)
        self.sc.pdp.require("connection:read", self.conns.ref_of(conn), hide_unless=None)
        if conn["connection_type"] != "model_api" or conn["adapter_key"] != body.adapter_key:
            raise ValidationFailed(
                "connection_id must reference a model_api connection of the same adapter"
            )
        if body.vendor_id is not None:
            vendors.require_active_vendor(self._session, self.sc.tenant_id, body.vendor_id)
        provider_id = uuid7()
        try:
            repository.insert_provider(
                self._session,
                {
                    "id": provider_id,
                    "tenant_id": self.sc.tenant_id,
                    "vendor_id": body.vendor_id,
                    "name": body.name,
                    "adapter_key": body.adapter_key,
                    "connection_id": body.connection_id,
                    "settings": body.settings,
                    "created_by": self.sc.principal.principal_id,
                },
            )
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise Conflict("a model provider with this name already exists") from exc
        row = self._provider_row(provider_id)
        self._audit(
            "model.provider.created",
            "model_provider",
            provider_id,
            f"provider:{body.name}",
            {"adapter": body.adapter_key, "connection_id": str(body.connection_id)},
        )
        self._commit("model provider")
        return _provider_out(row)

    def update_provider(self, provider_id: UUID, body: s.ProviderPatch) -> s.ProviderOut:
        self._manage()
        self._provider_row(provider_id, write=True)
        sent = body.model_fields_set - {"row_version"}
        values = {k: getattr(body, k) for k in sent if getattr(body, k) is not None}
        if not values:
            raise ValidationFailed("no updatable fields supplied")
        try:
            updated = repository.update_provider(
                self._session, provider_id, body.row_version, values, tenant_id=self.sc.tenant_id
            )
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise Conflict("a model provider with this name already exists") from exc
        if updated is None:
            raise Conflict("row_version mismatch: the provider was modified; reload and retry")
        self._audit(
            "model.provider.updated",
            "model_provider",
            provider_id,
            f"provider:{updated['name']}",
            {"changed": sorted(values)},
        )
        self._commit("model provider")
        return _provider_out(updated)

    def _provider_call(
        self, provider: Mapping[str, Any], purpose: str, deadline: float | None = None
    ) -> Any:  # Any: adapter+ctx
        conn = self.conns.get_row(provider["connection_id"])
        return self.conns.prepare_call(
            conn, purpose, require_usable=purpose != "model.health", deadline=deadline
        )

    def health_check(self, provider_id: UUID) -> s.HealthOut:
        self._manage()
        provider = self._provider_row(provider_id, write=True)
        adapter, ctx, cfg = self._provider_call(provider, "model.health")
        try:
            result = adapter.health(ctx, cfg)
            status, latency, detail = result.status, result.latency_ms, scrub_text(result.detail)
        except AdapterFailure as exc:
            status, latency, detail = "unavailable", 0, scrub_text(exc.message)
        checked = utcnow()
        repository.update_provider(
            self._session,
            provider_id,
            None,
            {"health_status": status, "health_checked_at": checked, "health_detail": detail[:500]},
            tenant_id=self.sc.tenant_id,
        )
        self._audit(
            "model.provider.health_checked",
            "model_provider",
            provider_id,
            f"provider:{provider['name']}",
            {"status": status, "latency_ms": latency},
        )
        self._commit("model provider")
        return s.HealthOut(
            status=status, latency_ms=latency, checked_at=checked, detail=detail or None
        )

    def discover_models(self, provider_id: UUID) -> Page[s.DiscoveredModelOut]:
        self._manage()
        provider = self._provider_row(provider_id, write=True)
        adapter, ctx, cfg = self._provider_call(provider, "model.discover")
        try:
            infos = adapter.list_models(ctx, cfg)
        except AdapterFailure as exc:
            raise failure_to_error(exc) from exc
        self._audit(
            "model.provider.models_discovered",
            "model_provider",
            provider_id,
            f"provider:{provider['name']}",
            {"count": len(infos)},
        )
        self._commit("model provider")
        return Page(
            items=[
                s.DiscoveredModelOut(
                    model_key=i.model_key,
                    display_name=i.display_name,
                    context_window=i.context_window,
                    max_output_tokens=i.max_output_tokens,
                    capabilities=list(i.capabilities),
                )
                for i in infos
            ]
        )

    # ================================================================== models
    def list_models(
        self, *, provider_id: UUID | None, lifecycle: str | None, limit: int, cursor: str | None
    ) -> Page[s.ModelOut]:
        self._read()
        rows = repository.list_models(
            self._session,
            tenant_id=self.sc.tenant_id,
            provider_id=provider_id,
            lifecycle=lifecycle,
            after=decode_cursor(cursor),
            limit=limit + 1,
            shared=self._shared(),
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[_model_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get_model(self, model_id: UUID) -> s.ModelOut:
        self._read()
        return _model_out(self._model_row(model_id))

    def create_model(self, body: s.ModelCreate) -> s.ModelOut:
        self._manage()
        self._provider_row(body.provider_id, write=True)
        model_id = uuid7()
        try:
            repository.insert_model(
                self._session,
                {
                    "id": model_id,
                    "tenant_id": self.sc.tenant_id,
                    "provider_id": body.provider_id,
                    "model_key": body.model_key,
                    "display_name": body.display_name,
                    "family": body.family,
                    "version_label": body.version_label,
                    "capabilities": list(body.capabilities),
                    "context_window": body.context_window,
                    "max_output_tokens": body.max_output_tokens,
                    "lifecycle": body.lifecycle,
                    "deprecation_date": body.deprecation_date,
                    "rate_limits": body.rate_limits.model_dump(exclude_none=True),
                    "pricing_ref": None,
                    "created_by": self.sc.principal.principal_id,
                },
            )
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise Conflict("this provider already has a model with that key") from exc
        row = self._model_row(model_id)
        self._audit(
            "model.created",
            "model",
            model_id,
            f"model:{body.model_key}",
            {"provider_id": str(body.provider_id), "lifecycle": body.lifecycle},
        )
        self._commit("model")
        return _model_out(row)

    def update_model(self, model_id: UUID, body: s.ModelPatch) -> s.ModelOut:
        self._manage()
        row = self._model_row(model_id, write=True)
        sent = body.model_fields_set - {"row_version"}
        if not sent:
            raise ValidationFailed("no updatable fields supplied")
        if row["lifecycle"] == "retired":
            raise Conflict("a retired model is terminal and cannot be modified")
        values: dict[str, Any] = {}
        for key in (
            "display_name",
            "family",
            "version_label",
            "context_window",
            "max_output_tokens",
        ):
            if key in sent and (
                getattr(body, key) is not None or key in ("family", "version_label")
            ):
                values[key] = getattr(body, key)
        if "capabilities" in sent and body.capabilities is not None:
            values["capabilities"] = list(body.capabilities)
        if "deprecation_date" in sent:
            values["deprecation_date"] = body.deprecation_date
        if "rate_limits" in sent and body.rate_limits is not None:
            values["rate_limits"] = body.rate_limits.model_dump(exclude_none=True)
        if (
            "lifecycle" in sent
            and body.lifecycle is not None
            and body.lifecycle != row["lifecycle"]
        ):
            if body.lifecycle not in LIFECYCLE_TRANSITIONS[row["lifecycle"]]:
                raise Conflict(f"a {row['lifecycle']} model cannot become {body.lifecycle}")
            values["lifecycle"] = body.lifecycle
        if not values:
            return _model_out(row)
        updated = repository.update_model(
            self._session, model_id, body.row_version, values, tenant_id=self.sc.tenant_id
        )
        if updated is None:
            raise Conflict("row_version mismatch: the model was modified; reload and retry")
        self._audit(
            "model.updated",
            "model",
            model_id,
            f"model:{row['model_key']}",
            {"changed": sorted(values), "lifecycle": updated["lifecycle"]},
        )
        self._commit("model")
        return _model_out(updated)

    # ================================================================== the SHI switch
    def get_shi_access(self) -> s.ShiAccessOut:
        """This client's own switch for the SHI provider (`model:read`; the owner sees it)."""
        self._read()
        return _access_out(repository.get_shi_access(self._session, tenant_id=self.sc.tenant_id))


# ====================================================================== platform administrator
def _platform_access_out(org: str, r: Mapping[str, Any] | None) -> s.PlatformShiAccessOut:
    base = _access_out(r)
    return s.PlatformShiAccessOut(
        **base.model_dump(), org=org, changed_by=r["changed_by"] if r is not None else None
    )


def _client_by_slug(session: Session, org: str) -> tuple[UUID, str]:
    """(id, status) of client `org`, looked up in the platform administrator context; an unknown
    slug gets `resolve_org`'s answer. Leaves the client's context set."""
    tenant_id = tenancy.tenant_id_by_slug(session, org)
    if tenant_id is None:
        raise NotFound(ORG_NOT_FOUND)
    db.set_tenant(session, tenant_id)
    header = tenancy.tenant_header(session, tenant_id)
    if header is None:
        raise NotFound(ORG_NOT_FOUND)
    return tenant_id, header[2]


def platform_get_shi_provider(session: Session, principal: Principal) -> s.ProviderOut:
    tenancy.require_platform_admin(session, principal)
    row = repository.get_shi_provider(session)
    if row is None:
        raise NotFound(SHI_NOT_CONFIGURED)
    return _provider_out(row)


def platform_put_shi_provider(
    session: Session,
    ctx: RequestContext,
    principal: Principal,
    registry: AdapterRegistry,
    body: s.ShiProviderPut,
) -> s.ProviderOut:
    """Create the SHI provider (first call, disabled unless `status` says otherwise) or change
    its name and status. Audited at installation level; no client is its subject."""
    tenancy.require_platform_admin(session, principal)
    desc = registry.descriptor(body.adapter_key)
    if desc is None or desc.kind != "model_provider":
        raise ValidationFailed(f"{body.adapter_key!r} is not a model provider adapter")
    try:
        with db.platform_admin_context(session):
            created = repository.get_shi_provider(session) is None
            row = repository.ensure_shi_provider(
                session,
                adapter_key=body.adapter_key,
                created_by=principal.principal_id,
                **({"name": body.name} if body.name is not None else {}),
            )
            if row["adapter_key"] != body.adapter_key:
                raise ValidationFailed("the SHI provider's adapter_key cannot change")
            values: dict[str, Any] = {}
            if body.name is not None and body.name != row["name"]:
                values["name"] = body.name
            if body.status is not None and body.status != row["status"]:
                values["status"] = body.status
            if not created and body.row_version not in (None, row["row_version"]):
                raise Conflict(
                    "row_version mismatch: the SHI provider was modified; reload and retry"
                )
            if values:
                updated = repository.update_provider(
                    session, repository.SHI_PROVIDER_ID, row["row_version"], values, tenant_id=None
                )
                if updated is None:
                    raise Conflict(
                        "row_version mismatch: the SHI provider was modified; reload and retry"
                    )
                row = updated
            audit.record(
                session,
                ctx,
                action=SHI_CONFIGURED,
                tenant_id=None,
                target_type="model_provider",
                target_id=repository.SHI_PROVIDER_ID,
                target_label=f"provider:{row['name']}",
                details={
                    "created": created,
                    "adapter": row["adapter_key"],
                    "status": row["status"],
                    "changed": sorted(values),
                },
            )
            session.commit()
    except BaseException:
        session.rollback()
        raise
    return _provider_out(row)


def platform_get_shi_access(
    session: Session, principal: Principal, org: str
) -> s.PlatformShiAccessOut:
    tenancy.require_platform_admin(session, principal)
    try:
        with db.platform_admin_context(session):
            tenant_id, _status = _client_by_slug(session, org)
            row = repository.get_shi_access(session, tenant_id=tenant_id)
        session.rollback()
    finally:
        db.set_tenant(session, None)
    return _platform_access_out(org, row)


def platform_set_shi_access(
    session: Session, ctx: RequestContext, principal: Principal, org: str, body: s.ShiAccessPut
) -> s.PlatformShiAccessOut:
    """Turn the SHI provider on or off for client `org` (design 5.3: off by default; only the
    platform administrator switches it). The change is recorded at installation level and in the
    client's own audit trail, shown to its owner. The administrator check comes first, so a
    refusal says nothing about `org`. An archived client can be switched off, not on."""
    tenancy.require_platform_admin(session, principal)
    try:
        with db.platform_admin_context(session):
            tenant_id, status = _client_by_slug(session, org)
            if body.enabled and status != "active":
                raise ScopeArchived("organization is archived")
            before = repository.get_shi_access(session, tenant_id=tenant_id)
            row = repository.set_shi_access(
                session, tenant_id=tenant_id, enabled=body.enabled, expected=body.row_version
            )
            if row is None:
                raise Conflict("row_version mismatch: the switch was modified; reload and retry")
            details = {
                "enabled": body.enabled,
                "previously_enabled": bool(before is not None and before["enabled"]),
                "by_platform_administrator": True,
            }
            audit.record(
                session,
                ctx,
                action=SHI_ACCESS_CHANGED,
                tenant_id=tenant_id,
                target_type="model_provider",
                target_id=repository.SHI_PROVIDER_ID,
                target_label="provider:SHI",
                details=details,
            )
            db.set_tenant(session, None)
            audit.record(
                session,
                ctx,
                action=SHI_ACCESS_CHANGED,
                tenant_id=None,
                target_type="tenant",
                target_id=tenant_id,
                target_label=f"org:{org}",
                details=details,
            )
            session.commit()
    except BaseException:
        session.rollback()
        db.set_tenant(session, None)
        raise
    db.set_tenant(session, None)
    return _platform_access_out(org, row)


__all__ = [
    "INVOCABLE",
    "LIFECYCLE_TRANSITIONS",
    "ModelService",
    "platform_get_shi_access",
    "platform_get_shi_provider",
    "platform_put_shi_provider",
    "platform_set_shi_access",
]
