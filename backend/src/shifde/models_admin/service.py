"""Model registry: providers and models of a client, and the SHI-wide provider with its per-client
switch.

Ported from SETH 3ba0e06 `models_admin/service.py`: lines 1-552 (providers, health check,
discovery, models; part 2a) and 553-924 (allowlists, effective models, cross-module checks, routes,
governed invoke; part 2b). Changes (DECISIONS B-1711..B-1719, B-1731..B-1737):

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
* The SHI call-time gate (B-1732): an invoke of a shared SHI model in a client whose switch is off
  is refused (answered like an unknown model) before any network call and audited in that client;
  checked at lookup and again right before the call. SHI's endpoint and key are installation
  settings, read only on that call path. Allowlists and routes name own models only (B-1733).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal  # Any: rows / provider JSON
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.adapters.base import (
    AdapterFailure,
    ChatMessage,
    ChatRequest,
    ConnectionConfig,
    Credential,
    ExecContext,
    ModelInfo,
    Usage,
)
from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.connections import egress
from shifde.connections.service import ERROR_CODE, ConnectionService, failure_to_error
from shifde.core import classification as data_class
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database
from shifde.core.errors import (
    AppError,
    BudgetExceeded,
    Conflict,
    Forbidden,
    ModelNotAllowed,
    ModelRateLimited,
    ModelUnavailable,
    NotFound,
    ScopeArchived,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.core.redaction import scrub, scrub_text
from shifde.ledger import enforcement, estimate, pricing
from shifde.models_admin import limiter, repository
from shifde.models_admin import schemas as s
from shifde.models_admin.schemas import ScopeType
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
SHI_OFF = "shi_provider_off"  # the reason a call-time gate refusal is audited with
# `shifde.jobs.kind` of a queued provider health check / model discovery (ARCH M2, B-2717)
HEALTH_JOB_KIND = "model.health_check"
DISCOVER_JOB_KIND = "model.discover"
# per kind: the dedupe key's prefix (`{prefix}:{provider_id}`) and the worker's request id
# (`{prefix}-{job_id}`), the correlation id of the audit event that holds the result
JOB_PREFIX = {HEALTH_JOB_KIND: "model-health-check", DISCOVER_JOB_KIND: "model-discover"}
JOB_ACTION = {
    HEALTH_JOB_KIND: "model.provider.health_checked",
    DISCOVER_JOB_KIND: "model.provider.models_discovered",
}
JOB_NOT_FOUND = "provider job not found"


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


def _route_out(r: Mapping[str, Any]) -> s.RouteOut:
    return s.RouteOut(
        id=r["id"],
        name=r["name"],
        scope=s.RouteScope(type=r["scope_type"], id=r["scope_id"]),
        primary_model_id=r["primary_model_id"],
        fallback_model_ids=list(r["fallback_model_ids"] or []),
        conditions=r["conditions"] or {},
        row_version=r["row_version"],
    )


@dataclass(frozen=True)
class RunInvoke:
    """A run's model call (`ModelService.invoke_for_run`): the answer, what the provider itself
    reported (`None` counts = not reported; the runtime estimates those and labels the usage
    estimated, carry_forward Item 11) and the output ceiling sent (Item 9)."""

    out: s.InvokeOut
    reported: Usage
    max_tokens: int


def _access_out(r: Mapping[str, Any] | None) -> s.ShiAccessOut:
    if r is None:  # no row: off (B-1701)
        return s.ShiAccessOut(enabled=False, changed_at=None, row_version=None)
    return s.ShiAccessOut(
        enabled=r["enabled"], changed_at=r["changed_at"], row_version=r["row_version"]
    )


def _job_state(
    job: Mapping[str, Any], found: Mapping[str, Any] | None
) -> tuple[Literal["queued", "running", "done", "failed"], str | None]:
    """A provider job's status and error code (`ConnectionService._job_out`'s mapping)."""
    if found is not None:
        return "done", None
    if job["status"] == "queued":
        return "queued", None
    if job["status"] == "leased":
        return "running", None
    # dead, or closed without a result (the reaper's `stale lease`)
    code = job["last_error"] or ""
    return "failed", code if ERROR_CODE.fullmatch(code) else "worker_lost"


def _discovered(i: ModelInfo) -> dict[str, object]:
    """A discovered model as its audit event stores it: `max_output_tokens` as `max_output`
    (`scrub` redacts a key naming tokens); `_discovery_job_out` maps it back."""
    return {
        "model_key": i.model_key,
        "display_name": i.display_name,
        "context_window": i.context_window,
        "max_output": i.max_output_tokens,
        "capabilities": list(i.capabilities),
    }


class ModelService:
    def __init__(
        self, scope: TenantScope, runtime: IntegrationRuntime, db: Database | None = None
    ) -> None:
        """`db` (the request's database) lets an invoke write its usage row in its own
        transaction the moment the provider answers (carry_forward Item 11)."""
        self.sc = scope
        self.runtime = runtime
        self.db = db
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

    def provider_owner(self, provider_id: UUID) -> Literal["own", "shared"] | None:
        """`own` for a provider of this client, `shared` for the shared SHI provider (tenant
        NULL), None for an unknown id and another client's alike (no permission check)."""
        row = repository.get_provider(
            self._session, provider_id, tenant_id=self.sc.tenant_id, shared=True
        )
        if row is None:
            return None
        if row["tenant_id"] is None:
            return "shared"
        return "own" if row["tenant_id"] == self.sc.tenant_id else None

    def own_model(self, model_id: UUID) -> bool:
        """Whether `model_id` is a model of this client (False for a shared SHI model, another
        client's and an unknown id; no permission check)."""
        row = repository.get_model(
            self._session, model_id, tenant_id=self.sc.tenant_id, shared=False
        )
        return row is not None and row["tenant_id"] == self.sc.tenant_id

    def own_model_by_key(self, provider_id: UUID, model_key: str) -> UUID | None:
        """The id of this client's own model `model_key` served by `provider_id`; None when there
        is none (a shared SHI model, another client's and an unknown pair alike; no permission
        check)."""
        return repository.own_model_id_by_key(
            self._session, provider_id, model_key, tenant_id=self.sc.tenant_id
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

    # ================================================================== provider jobs (ARCH M2)
    def _job_target(self, provider_id: UUID) -> Mapping[str, Any]:
        """The provider a health check or discovery may be queued for: `model:manage` (403), an
        active client (409), an own provider (404; 403 for the shared one), its connection (404)
        and an implemented adapter (422). No secret is resolved and no adapter is called."""
        self._manage()
        provider = self._provider_row(provider_id, write=True)
        self.conns.require_implemented(self.conns.get_row(provider["connection_id"]))
        return provider

    def _enqueue(self, kind: str, provider_id: UUID) -> Mapping[str, Any]:
        self._job_target(provider_id)
        p = self.sc.principal
        job = repository.enqueue_provider_job(
            self._session,
            tenant_id=self.sc.tenant_id,
            kind=kind,
            job_id=uuid7(),
            dedupe_key=f"{JOB_PREFIX[kind]}:{provider_id}",
            payload={
                "provider_id": str(provider_id),
                "principal_id": str(p.principal_id),
                "principal_type": p.principal_type,
                "api_key_id": str(p.api_key_id) if p.api_key_id else None,
            },
        )
        self._session.commit()
        return job

    def _job(self, kind: str, provider_id: UUID, job_id: UUID) -> Mapping[str, Any]:
        """The `kind` job `job_id` of a provider of this client; one 404 for every miss (unknown
        job, another client's, another provider's or of the other kind, unknown provider)."""
        self._read()
        try:
            self._provider_row(provider_id)
        except NotFound as exc:
            raise NotFound(JOB_NOT_FOUND) from exc
        job = repository.get_provider_job(
            self._session,
            job_id,
            tenant_id=self.sc.tenant_id,
            kinds=(kind,),
            provider_id=provider_id,
        )
        if job is None:
            raise NotFound(JOB_NOT_FOUND)
        return job

    def _job_details(self, job: Mapping[str, Any]) -> dict[str, Any] | None:
        """The result a provider job recorded: its audit event's details (B-2717)."""
        kind = job["kind"]
        return audit.find_details(
            self._session,
            tenant_id=self.sc.tenant_id,
            action=JOB_ACTION[kind],
            target_id=UUID(job["payload"]["provider_id"]),
            correlation_id=f"{JOB_PREFIX[kind]}-{job['id']}",
            occurred_from=job["created_at"],
        )

    def _health_job_out(self, job: Mapping[str, Any]) -> s.HealthCheckJobOut:
        found = self._job_details(job)
        status, error = _job_state(job, found)
        return s.HealthCheckJobOut(
            id=job["id"],
            provider_id=UUID(job["payload"]["provider_id"]),
            status=status,
            result=(
                s.HealthOut(
                    status=found["status"],
                    latency_ms=found["latency_ms"],
                    checked_at=datetime.fromisoformat(found["checked_at"]),
                    detail=found["detail"],
                )
                if found is not None
                else None
            ),
            error=error,
            created_at=job["created_at"],
        )

    def _discovery_job_out(self, job: Mapping[str, Any]) -> s.DiscoveryJobOut:
        found = self._job_details(job)
        status, error = _job_state(job, found)
        return s.DiscoveryJobOut(
            id=job["id"],
            provider_id=UUID(job["payload"]["provider_id"]),
            status=status,
            result=(
                [
                    s.DiscoveredModelOut(
                        model_key=m["model_key"],
                        display_name=m["display_name"],
                        context_window=m["context_window"],
                        max_output_tokens=m["max_output"],  # stored renamed (`_discovered`)
                        capabilities=m["capabilities"],
                    )
                    for m in found["models"]
                ]
                if found is not None
                else None
            ),
            error=error,
            created_at=job["created_at"],
        )

    def enqueue_health_check(self, provider_id: UUID) -> s.HealthCheckJobOut:
        """Queue the provider's health check on the worker (ARCH M2, B-2717): the API process
        never calls the adapter. An active health check job of the provider is answered instead
        of a second one."""
        return self._health_job_out(self._enqueue(HEALTH_JOB_KIND, provider_id))

    def enqueue_discovery(self, provider_id: UUID) -> s.DiscoveryJobOut:
        """Queue the provider's model discovery on the worker, as `enqueue_health_check`."""
        return self._discovery_job_out(self._enqueue(DISCOVER_JOB_KIND, provider_id))

    def health_check_job(self, provider_id: UUID, job_id: UUID) -> s.HealthCheckJobOut:
        return self._health_job_out(self._job(HEALTH_JOB_KIND, provider_id, job_id))

    def discovery_job(self, provider_id: UUID, job_id: UUID) -> s.DiscoveryJobOut:
        return self._discovery_job_out(self._job(DISCOVER_JOB_KIND, provider_id, job_id))

    def execute_health_check(self, provider_id: UUID) -> None:
        """Run the health check and record it (the worker side of `enqueue_health_check`): the
        provider's health columns and the `model.provider.health_checked` event, which carries
        the result under this context's request id."""
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
            {
                "status": status,
                "latency_ms": latency,
                "checked_at": checked.isoformat(),
                "detail": detail or None,
            },
        )
        self._commit("model provider")

    def execute_discovery(self, provider_id: UUID) -> None:
        """Run the model discovery and record it (the worker side of `enqueue_discovery`): the
        `model.provider.models_discovered` event, which carries the models under this context's
        request id. An adapter failure raises its mapped error; nothing is recorded then."""
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
            {"count": len(infos), "models": [_discovered(i) for i in infos]},
        )
        self._commit("model provider")

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

    # ================================================================== budgets and usage
    def _enforce_budgets(
        self,
        ref: ResourceRef,
        model: Mapping[str, Any],
        est_input: int,
        max_tokens: int,
        is_test: bool,
        agent_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> UUID | None:
        """Budgets and token quotas BEFORE a direct invocation (outside a run): the tenant /
        workspace / project / agent / principal budgets covering the caller. The call's tokens
        (and estimated cost) are then held through `ledger.reservations` in the same transaction,
        while the rule locks are held, keyed by a new invocation; the hold names every budget and
        token quota it was checked against. Returns the hold's id, None when no rule applies.
        The output side is the `max_tokens` sent to the provider (carry_forward Item 9)."""
        scope = enforcement.BudgetScope(
            self.sc.tenant_id,
            ref.workspace_id,
            ref.project_id,
            agent_id,
            None,
            None,
            self.sc.principal.principal_id,
        )
        try:
            ev = enforcement.evaluate(
                self._session,
                scope,
                estimate.call_hold_tokens(est_input, max_tokens),
                enforcement.ReserveDetail(model["id"], est_input, max_tokens),
            )
        except enforcement.BudgetDenied as exc:
            self._session.rollback()
            authz.insert_decisions(
                self._session,
                [
                    {
                        "id": uuid7(),
                        "tenant_id": self.sc.tenant_id,
                        "at": utcnow(),
                        "principal_id": self.sc.principal.principal_id,
                        "principal_type": self.sc.principal.principal_type,
                        "action": "model:invoke",
                        "resource_type": "model",
                        "resource_id": str(model["id"]),
                        "decision": "deny",
                        "reason_codes": ["budget_denied"],
                        "matched_binding_ids": [],
                        "policy_ids": [exc.budget_id] if exc.budget_id else [],
                        "run_id": run_id,
                        "trace_id": self.sc.ctx.request_id,
                    }
                ],
            )
            self._invoked(
                model,
                ref,
                run_id,
                {
                    "reason": "budget_denied",
                    "detail": exc.message,
                    "test": is_test,
                    **({"policy_id": str(exc.budget_id)} if exc.budget_id else {}),
                },
                outcome="denied",
            )
            self._session.commit()
            raise BudgetExceeded(exc.message) from exc
        budget_ids = [*ev.applicable, *ev.quota_ids]
        if not budget_ids:
            return None
        return ledger.reservations.reserve_invocation(
            self._session,
            self.sc.tenant_id,
            budget_ids,
            ev.hold_tokens,
            ev.est_cost if ev.est_cost is not None else Decimal(0),
            ev.est_currency,
        )

    def _release_hold(self, hold: UUID | None) -> None:
        """Drop a direct invocation's hold whose provider call was never sent (committed)."""
        if hold is None:
            return
        if self._session.in_transaction() and not self._session.is_active:
            self._session.rollback()
        ledger.reservations.release(self._session, self.sc.tenant_id, hold)
        self._session.commit()

    def _settle_hold(self, hold: UUID | None, tokens: int, cost: Decimal | None) -> None:
        """Replace a direct invocation's hold by its recorded usage (the caller commits)."""
        if hold is not None:
            ledger.reservations.settle(self._session, self.sc.tenant_id, hold, tokens, cost)

    def _usage_values(
        self,
        ref: ResourceRef,
        model_id: UUID,
        latency_ms: int,
        event_type: str,
        is_test: bool,
        agent_id: UUID | None,
        *,
        succeeded: bool,
    ) -> dict[str, Any]:  # Any: column values
        return {
            "tenant_id": self.sc.tenant_id,
            "event_key": f"invoke:{uuid7()}",
            "event_type": event_type,
            "status": "succeeded" if succeeded else "failed",
            "workspace_id": ref.workspace_id,
            "project_id": ref.project_id,
            "principal_id": self.sc.principal.principal_id,
            "agent_id": agent_id,
            "model_id": model_id,
            "is_test": is_test,
            "call_count": 1,
            "duration_ms": latency_ms,
        }

    def _record_success(
        self,
        values: Mapping[str, Any],
        reported: Any,  # Any: adapter usage (input_tokens / output_tokens, None = not reported)
        est_input: int,
        output: str,
    ) -> ledger.EventResult:
        """One usage event for a model call that answered (FR-COST-001), priced from the rate
        card in force; written in its own committed transaction straight after the provider
        answered when the service has the database (carry_forward Item 11), else in the
        caller's transaction."""
        est_output = estimate.estimate_output_tokens(output)
        if self.db is not None:
            return ledger.record_model_call_durably(
                self.db,
                self.sc.tenant_id,
                values,
                reported=reported,
                estimated_input=est_input,
                estimated_output=est_output,
            )
        return ledger.record_model_call(
            self._session,
            values,
            reported=reported,
            estimated_input=est_input,
            estimated_output=est_output,
        )

    def _record_failure(self, values: Mapping[str, Any]) -> ledger.EventResult:
        """A failed attempt: `status="failed"` with 0 counts, as SETH."""
        costed, provider_key, model_key = ledger.price_model_usage(
            self._session,
            self.sc.tenant_id,
            values["model_id"],
            pricing.Usage(
                input_tokens=0, output_tokens=0, calls=0, duration_ms=values["duration_ms"]
            ),
        )
        return ledger.record(
            self._session,
            {
                **values,
                "provider_key": provider_key,
                "model_key": model_key,
                "input_tokens": 0,
                "output_tokens": 0,
                "provider_reported_usage": {"input": 0, "output": 0},
                "costed": costed,
            },
        )

    # ================================================================== allowlists
    def _chain(self, ref: ResourceRef) -> list[tuple[str, UUID]]:
        chain: list[tuple[str, UUID]] = [("tenant", ref.tenant_id)]
        if ref.workspace_id:
            chain.append(("workspace", ref.workspace_id))
        if ref.project_id:
            chain.append(("project", ref.project_id))
        return chain

    def allowed_model_ids(self, ref: ResourceRef) -> set[UUID] | None:
        """Intersection of the enabled allowlists down the scope chain; None = unrestricted."""
        allowed: set[UUID] | None = None
        for scope_type, scope_id in self._chain(ref):
            row = repository.get_allowlist(
                self._session, scope_type, scope_id, tenant_id=self.sc.tenant_id
            )
            if row is not None and row["enabled"]:
                ids = set(
                    repository.allowlist_model_ids(
                        self._session, row["id"], tenant_id=self.sc.tenant_id
                    )
                )
                allowed = ids if allowed is None else allowed & ids
        return allowed

    def _own_models(self, ids: Sequence[UUID]) -> dict[UUID, Mapping[str, Any]]:
        """This client's own models among `ids`. A shared SHI model is not one (allowlists and
        routes name own models only, B-1733), nor is another client's: both are left out, so the
        caller answers them like an unknown id (422) before any key is checked (review 1 item 6).
        """
        rows = repository.get_models(self._session, ids, tenant_id=self.sc.tenant_id, shared=False)
        return {r["id"]: r for r in rows if r["tenant_id"] == self.sc.tenant_id}

    def get_allowlist(self, scope_type: ScopeType, scope_id: UUID) -> s.AllowlistOut:
        ref = tenancy.resolve_scope_ref(self.sc, scope_type, scope_id)
        self._read(ref)
        row = repository.get_allowlist(
            self._session, scope_type, scope_id, tenant_id=self.sc.tenant_id
        )
        ids = (
            repository.allowlist_model_ids(self._session, row["id"], tenant_id=self.sc.tenant_id)
            if row
            else []
        )
        return s.AllowlistOut(
            enabled=bool(row and row["enabled"]),
            model_ids=sorted(ids),
            scope_type=scope_type,
            scope_id=scope_id,
        )

    def put_allowlist(
        self, scope_type: ScopeType, scope_id: UUID, body: s.AllowlistPut
    ) -> s.AllowlistOut:
        ref = tenancy.resolve_scope_ref(self.sc, scope_type, scope_id)
        self._manage(ref)
        unique = sorted(set(body.model_ids))
        if set(self._own_models(unique)) != set(unique):
            raise ValidationFailed("model_ids must all be models of this organization")
        allowlist_id = repository.upsert_allowlist(
            self._session, self.sc.tenant_id, uuid7(), scope_type, scope_id, body.enabled
        )
        repository.replace_allowlist_models(self._session, self.sc.tenant_id, allowlist_id, unique)
        self._audit(
            "model.allowlist.updated",
            "model_allowlist",
            allowlist_id,
            f"allowlist:{scope_type}:{scope_id}",
            {"enabled": body.enabled, "model_count": len(unique), "scope_type": scope_type},
        )
        self._commit("allowlist")
        return s.AllowlistOut(
            enabled=body.enabled, model_ids=unique, scope_type=scope_type, scope_id=scope_id
        )

    # ================================================================== effective models
    def _providers(self) -> dict[UUID, Mapping[str, Any]]:
        rows = repository.list_providers(
            self._session, None, 1000, tenant_id=self.sc.tenant_id, shared=self._shared()
        )
        return {p["id"]: p for p in rows}

    def _usable(self, model: Mapping[str, Any], providers: dict[UUID, Mapping[str, Any]]) -> bool:
        provider = providers.get(model["provider_id"])
        return (
            model["lifecycle"] in INVOCABLE
            and provider is not None
            and provider["status"] == "active"
            and provider["health_status"] != "unavailable"
        )

    def effective_models(
        self, workspace: str | None, project: str | None, limit: int, cursor: str | None
    ) -> Page[s.ModelOut]:
        ref = tenancy.scope_ref_from_slugs(self.sc, workspace, project)
        self._read(ref)
        allowed = self.allowed_model_ids(ref)
        providers = self._providers()
        rows = repository.list_models(
            self._session,
            tenant_id=self.sc.tenant_id,
            provider_id=None,
            lifecycle=None,
            after=decode_cursor(cursor),
            limit=1000,
            shared=self._shared(),
        )
        usable = [
            r
            for r in rows
            if (allowed is None or r["id"] in allowed) and self._usable(r, providers)
        ]
        page, more = paginate(usable, limit)
        return Page(
            items=[_model_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    # ================================================================== cross-module checks
    def problems_at(self, ref: ResourceRef, model_ids: Sequence[UUID]) -> dict[UUID, str]:
        """{model id: problem code} for models that cannot be used at `ref`: missing from this
        organization (another client's, or a shared SHI model while the client's switch is off),
        not usable (lifecycle / provider status / health) or not on the allowlist chain
        (tenant > workspace > project). Authorization is the caller's job."""
        ids = list(dict.fromkeys(model_ids))
        rows = repository.get_models(
            self._session, ids, tenant_id=self.sc.tenant_id, shared=self._shared()
        )
        found = {r["id"]: r for r in rows}
        providers = self._providers()
        allowed = self.allowed_model_ids(ref)
        problems: dict[UUID, str] = {}
        for model_id in ids:
            model = found.get(model_id)
            if model is None or model["tenant_id"] not in (self.sc.tenant_id, None):
                problems[model_id] = "model_not_found"
            elif not self._usable(model, providers):
                problems[model_id] = "model_unavailable"
            elif allowed is not None and model_id not in allowed:
                problems[model_id] = "model_not_allowed"
        return problems

    def route_model_ids(
        self, route_id: UUID, *, at: tuple[UUID | None, UUID | None] | None = None
    ) -> list[UUID] | None:
        """Primary then fallback model ids of a route of this organization (None if unknown).
        `at` (a run's own (workspace id, project id)): only a route of the client level, of that
        workspace or of that project; any other route of the client answers None, like an unknown
        id (runtime isolation review 6 finding 4)."""
        tenant = self.sc.tenant_id
        row = repository.get_route(self._session, route_id, tenant_id=tenant)
        if row is None or row["tenant_id"] != tenant:
            return None
        if at is not None:
            workspace_id, project_id = at
            allowed: set[tuple[str, UUID]] = {("tenant", tenant)}
            if workspace_id is not None:
                allowed.add(("workspace", workspace_id))
            if project_id is not None:
                allowed.add(("project", project_id))
            if (row["scope_type"], row["scope_id"]) not in allowed:
                return None
        return [row["primary_model_id"], *list(row["fallback_model_ids"] or [])]

    # ================================================================== routes
    def list_routes(self, limit: int, cursor: str | None) -> Page[s.RouteOut]:
        self._read()
        rows = repository.list_routes(
            self._session, decode_cursor(cursor), limit + 1, tenant_id=self.sc.tenant_id
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[_route_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get_route(self, route_id: UUID) -> s.RouteOut:
        self._read()
        return _route_out(self._route_row(route_id))

    def _route_row(self, route_id: UUID) -> Mapping[str, Any]:
        row = repository.get_route(self._session, route_id, tenant_id=self.sc.tenant_id)
        if row is None or row["tenant_id"] != self.sc.tenant_id:
            raise NotFound("model route not found")
        return row

    def _validate_route(self, ref: ResourceRef, primary: UUID, fallbacks: list[UUID]) -> None:
        """The primary and every fallback: distinct models of this client (review 1 item 6:
        checked here, 422, before any key), usable, and allowed at the route's scope."""
        ids = [primary, *fallbacks]
        if len(set(ids)) != len(ids):
            raise ValidationFailed("the primary and fallback models must be distinct")
        found = self._own_models(ids)
        if any(i not in found for i in ids):
            raise ValidationFailed("every route model must be a model of this organization")
        providers = self._providers()
        allowed = self.allowed_model_ids(ref)
        for model_id in ids:
            model = found[model_id]
            if not self._usable(model, providers):
                raise ValidationFailed(
                    f"model {model['model_key']!r} is not usable "
                    "(lifecycle, provider status or health)"
                )
            if allowed is not None and model_id not in allowed:
                raise ValidationFailed(
                    f"model {model['model_key']!r} is not allowed at the route's scope"
                )

    def create_route(self, body: s.RouteCreate) -> s.RouteOut:
        ref = tenancy.resolve_scope_ref(self.sc, body.scope.type, body.scope.id)
        self._manage(ref)
        self._validate_route(ref, body.primary_model_id, body.fallback_model_ids)
        route_id = uuid7()
        try:
            repository.insert_route(
                self._session,
                {
                    "id": route_id,
                    "tenant_id": self.sc.tenant_id,
                    "name": body.name,
                    "scope_type": body.scope.type,
                    "scope_id": body.scope.id,
                    "primary_model_id": body.primary_model_id,
                    "fallback_model_ids": body.fallback_model_ids,
                    "conditions": body.conditions,
                    "created_by": self.sc.principal.principal_id,
                },
            )
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise Conflict("a model route with this name already exists") from exc
        row = self._route_row(route_id)
        self._audit(
            "model.route.created",
            "model_route",
            route_id,
            f"route:{body.name}",
            {"scope_type": body.scope.type, "fallbacks": len(body.fallback_model_ids)},
        )
        self._commit("model route")
        return _route_out(row)

    def update_route(self, route_id: UUID, body: s.RoutePatch) -> s.RouteOut:
        row = self._route_row(route_id)
        ref = tenancy.resolve_scope_ref(self.sc, row["scope_type"], row["scope_id"])
        self._manage(ref)
        sent = body.model_fields_set - {"row_version"}
        values: dict[str, Any] = {k: getattr(body, k) for k in sent if getattr(body, k) is not None}
        if not values:
            raise ValidationFailed("no updatable fields supplied")
        self._validate_route(
            ref,
            values.get("primary_model_id", row["primary_model_id"]),
            values.get("fallback_model_ids", list(row["fallback_model_ids"] or [])),
        )
        try:
            updated = repository.update_route(
                self._session, route_id, body.row_version, values, tenant_id=self.sc.tenant_id
            )
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise Conflict("a model route with this name already exists") from exc
        if updated is None:
            raise Conflict("row_version mismatch: the route was modified; reload and retry")
        self._audit(
            "model.route.updated",
            "model_route",
            route_id,
            f"route:{updated['name']}",
            {"changed": sorted(values)},
        )
        self._commit("model route")
        return _route_out(updated)

    # ================================================================== governed invoke
    def _capture_full(self) -> bool:
        settings = tenancy.effective_settings(self.sc)
        mode = settings.get("capture_content")
        if mode is None:
            mode = "full" if self.runtime.settings.is_development else "metadata"
        return bool(mode == "full")

    def _invoked(
        self,
        model: Mapping[str, Any],
        ref: ResourceRef,
        run_id: UUID | None,
        details: dict[str, object],
        outcome: str = "success",
        label: str | None = None,
    ) -> None:
        """`model.invoked` in the caller's client, with the run / workspace / project context
        where it is known (carry_forward Item 15)."""
        context: dict[str, object] = {
            "workspace_id": str(ref.workspace_id) if ref.workspace_id else None,
            "project_id": str(ref.project_id) if ref.project_id else None,
            "run_id": str(run_id) if run_id else None,
        }
        self._audit(
            "model.invoked",
            "model",
            model["id"],
            label or f"model:{model['model_key']}",
            {**details, **context},
            outcome=outcome,
        )

    def _trust_gate(
        self,
        model: Mapping[str, Any],
        provider: Mapping[str, Any],
        ref: ResourceRef,
        run_id: UUID | None,
        classification: str,
    ) -> None:
        """Classification x provider trust (ARCH M11, design 5.3): a run whose data classification
        ranks above the provider's `settings.trust_level` (missing or invalid = `public`, so the
        shared SHI provider is `public`) is refused before any network call, audited with the two
        level names only, never what raised the level."""
        settings = provider["settings"]
        trust = data_class.level(
            settings.get("trust_level") if isinstance(settings, dict) else None
        )
        level = data_class.level(classification)
        if data_class.rank(level) <= data_class.rank(trust):
            return
        details: dict[str, object] = {
            "reason": "classification_above_provider_trust",
            "classification": level,
            "provider_trust_level": trust,
        }
        self._invoked(model, ref, run_id, details, outcome="denied")
        self._session.commit()
        raise ModelNotAllowed(
            "the run's data classification is above the model provider's trust level"
        )

    def _shi_gate(self, model: Mapping[str, Any], ref: ResourceRef, run_id: UUID | None) -> None:
        """The SHI call-time gate (design 5.3, plan Review Focus 5): a shared SHI model is called
        only while this client's switch is on. A refusal is audited in this client (labelled with
        the SHI provider, not the model) and answered like an unknown model, before any network
        call. Checked when the model is looked up and again right before the call."""
        if model["tenant_id"] is not None or self._shared():
            return
        self._session.rollback()
        self._invoked(
            model, ref, run_id, {"reason": SHI_OFF}, outcome="denied", label="provider:SHI"
        )
        self._session.commit()
        raise NotFound("model not found")

    def _invocable_model(
        self, model_id: UUID, ref: ResourceRef, run_id: UUID | None
    ) -> Mapping[str, Any]:
        row = repository.get_model(
            self._session, model_id, tenant_id=self.sc.tenant_id, shared=True
        )
        if row is not None and row["tenant_id"] is None:
            self._shi_gate(row, ref, run_id)
        return self._owned(row, "model", write=False)

    def _shi_call(
        self,
        provider: Mapping[str, Any],
        model: Mapping[str, Any],
        ref: ResourceRef,
        run_id: UUID | None,
        deadline: float,
    ) -> tuple[Any, ExecContext, ConnectionConfig]:  # Any: adapter instance
        """(adapter, context, config) of a call on the shared SHI provider. Its endpoint and key
        are installation configuration (`SHIFDE_SHI_PROVIDER_CONFIG`,
        `SHIFDE_SHI_PROVIDER_API_KEY`; B-1712, B-1732), read only here, on the call path; the
        call leaves under the caller's egress layers (installation and client defaults)."""
        self._shi_gate(model, ref, run_id)
        settings = self.runtime.settings
        if settings.shi_provider_config is None:
            raise ModelUnavailable("the SHI provider is not configured on this installation")
        desc = self.runtime.registry.descriptor(provider["adapter_key"])
        if desc is None or desc.kind != "model_provider" or not desc.implemented:
            raise ModelUnavailable("the SHI provider's adapter is not available")
        key = settings.shi_provider_api_key
        auth = next((m for m in desc.auth_methods if m != "none"), "none") if key else "none"
        cfg = ConnectionConfig(
            id=None,
            name="SHI",
            adapter_key=provider["adapter_key"],
            config=dict(settings.shi_provider_config),
            auth_method=auth,
            credential=Credential(key.get_secret_value()) if key else None,
        )
        guard = egress.build_guard(self._session, self.runtime, self.sc.tenant_id, None)
        guard.set_deadline(deadline)
        ctx = ExecContext(
            tenant_id=self.sc.tenant_id,
            principal_id=self.sc.principal.principal_id,
            egress=guard,
            request_id=self.sc.ctx.request_id,
            run_id=run_id,
        )
        return self.runtime.registry.get(provider["adapter_key"]), ctx, cfg

    def invoke(
        self,
        model_id: UUID,
        body: s.InvokeIn,
        workspace: str | None = None,
        project: str | None = None,
    ) -> s.InvokeOut:
        ref = tenancy.scope_ref_from_slugs(self.sc, workspace, project)
        return self.invoke_at(ref, model_id, body)

    def invoke_at(
        self,
        ref: ResourceRef,
        model_id: UUID,
        body: s.InvokeIn,
        *,
        max_deadline: float | None = None,
        record_usage: bool = True,
        usage_event_type: str = "model_call",
        is_test: bool = False,
        agent_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> s.InvokeOut:
        return self._invoke_at(
            ref,
            model_id,
            body,
            max_deadline=max_deadline,
            record_usage=record_usage,
            usage_event_type=usage_event_type,
            is_test=is_test,
            agent_id=agent_id,
            run_id=run_id,
        ).out

    def invoke_for_run(
        self,
        ref: ResourceRef,
        model_id: UUID,
        body: s.InvokeIn,
        *,
        run_id: UUID,
        max_deadline: float | None = None,
        classification: str | None = None,
    ) -> RunInvoke:
        """The runtime's governed call (one per attempt): `invoke_at` with `record_usage=False`
        (the run journals its own per-attempt usage event) and `run_id` on `model.invoked`
        (Item 15). `body.max_tokens` is required (the run's budget hold covers it, Item 9) and is
        only ever lowered, to the model's `max_output_tokens`. `classification` is the run's data
        classification: a provider whose trust level is below it is refused (ARCH M11)."""
        if body.max_tokens is None:
            raise ValueError("a run's model call needs an output ceiling (max_tokens)")
        return self._invoke_at(
            ref,
            model_id,
            body,
            max_deadline=max_deadline,
            record_usage=False,
            run_id=run_id,
            cap_to_model=True,
            classification=classification,
        )

    def _invoke_at(
        self,
        ref: ResourceRef,
        model_id: UUID,
        body: s.InvokeIn,
        *,
        max_deadline: float | None = None,
        record_usage: bool = True,
        usage_event_type: str = "model_call",
        is_test: bool = False,
        agent_id: UUID | None = None,
        run_id: UUID | None = None,
        cap_to_model: bool = False,
        classification: str | None = None,
    ) -> RunInvoke:
        """Governed invoke at an already-resolved scope (prompt / agent tests, the runtime: one
        call per attempt, a route's primary and every fallback alike, so the SHI gate holds on
        each). `agent_id` (an agent test) puts the agent in the budget scope and on the usage
        event; `run_id` (the runtime) goes on the audit event. `max_deadline` (seconds) can only
        shorten the call deadline. Every invocation writes a usage event (priced from the rate
        card in force) unless the caller journals its own per-attempt events
        (`record_usage=False`: the run runtime)."""
        model = self._invocable_model(model_id, ref, run_id)
        self.sc.pdp.require("model:invoke", ref, hide_unless="model:read")
        provider = self._provider_row(model["provider_id"])
        if model["lifecycle"] not in INVOCABLE:
            raise ModelUnavailable(
                f"model is {model['lifecycle']}; only preview/active models can be invoked"
            )
        if provider["status"] != "active":
            raise ModelUnavailable("the model provider is disabled")
        if provider["health_status"] == "unavailable":
            raise ModelUnavailable("the model provider is unavailable (last health check failed)")
        allowed = self.allowed_model_ids(ref)
        if allowed is not None and model_id not in allowed:
            self._invoked(model, ref, run_id, {"reason": "not_in_allowlist"}, outcome="denied")
            self._session.commit()
            raise ModelNotAllowed("the model is not on the allowlist for this scope")
        if classification is not None:
            self._trust_gate(model, provider, ref, run_id, classification)

        limits = model["rate_limits"] or {}
        bucket = f"model:{model_id}"
        rate_estimate = sum(len(m.content) for m in body.messages) // 4 + 1
        est_input = estimate.estimate_input_tokens(
            [{"role": m.role, "content": m.content} for m in body.messages]
        )
        reserve = self.runtime.settings.runtime_output_reserve_tokens
        max_tokens = body.max_tokens or min(reserve, model["max_output_tokens"] or reserve)
        if cap_to_model and model["max_output_tokens"]:
            max_tokens = min(max_tokens, int(model["max_output_tokens"]))
        hold: UUID | None = None  # run calls are reserved by the Execution Gate instead
        if record_usage:
            hold = self._enforce_budgets(
                ref, model, est_input, max_tokens, is_test, agent_id, run_id
            )
        lease: UUID | None = None
        deadline = self.runtime.settings.model_call_deadline_seconds
        if max_deadline is not None:
            deadline = max(min(deadline, max_deadline), 0.001)
        started = time.monotonic()
        try:
            limiter.check_tokens(
                self._session, self.sc.tenant_id, bucket + ":tpm", limits.get("tpm"), rate_estimate
            )
            limiter.take_request(
                self._session, self.sc.tenant_id, bucket + ":rpm", limits.get("rpm")
            )
            lease = limiter.acquire_lease(
                self._session,
                self.sc.tenant_id,
                bucket,
                limits.get("max_concurrency"),
                ttl_seconds=deadline + 30,  # outlives the hard call deadline
            )
            if hold is not None:  # the call may reach the provider from here: never freed
                ledger.reservations.mark_provider_request(self._session, self.sc.tenant_id, hold)
            # counters / lease (and the marked hold) committed BEFORE the provider call
            self._session.commit()
        except ModelRateLimited:
            self._session.rollback()
            self._invoked(model, ref, run_id, {"reason": "rate_limited"}, outcome="denied")
            self._session.commit()
            raise
        try:
            try:
                if provider["tenant_id"] is None:
                    adapter, ctx, cfg = self._shi_call(provider, model, ref, run_id, deadline)
                else:
                    adapter, ctx, cfg = self._provider_call(
                        provider, "model.invoke", deadline=deadline
                    )
                request = ChatRequest(
                    [ChatMessage(m.role, m.content) for m in body.messages], max_tokens
                )
            except Exception:
                self._release_hold(hold)  # refused before anything was sent
                raise
            try:
                response = adapter.generate(ctx, cfg, model["model_key"], request)
            except AdapterFailure as exc:
                latency = int((time.monotonic() - started) * 1000)
                if record_usage:
                    failed = self._record_failure(
                        self._usage_values(
                            ref,
                            model_id,
                            latency,
                            usage_event_type,
                            is_test,
                            agent_id,
                            succeeded=False,
                        )
                    )
                    self._settle_hold(hold, 0, failed.costed.amount)
                self._invoked(
                    model,
                    ref,
                    run_id,
                    {"error_class": exc.error_class.value, "latency_ms": latency},
                    outcome="failure",
                )
                self._session.commit()
                raise failure_to_error(exc) from exc
            latency = int((time.monotonic() - started) * 1000)
            recorded: ledger.EventResult | None = None
            if record_usage:  # the usage row first, while nothing of this transaction is pending
                recorded = self._record_success(
                    self._usage_values(
                        ref, model_id, latency, usage_event_type, is_test, agent_id, succeeded=True
                    ),
                    response.usage,
                    est_input,
                    response.output,
                )
            used = estimate.resolve_usage(
                response.usage,
                estimated_input=est_input,
                estimated_output=estimate.estimate_output_tokens(response.output),
            )
            if recorded is not None:
                self._settle_hold(
                    hold, used.input_tokens + used.output_tokens, recorded.costed.amount
                )
            limiter.add_tokens(
                self._session,
                self.sc.tenant_id,
                bucket + ":tpm",
                used.input_tokens + used.output_tokens,
            )
            details: dict[str, object] = {
                "provider_id": str(provider["id"]),
                "model_key": model["model_key"],
                # keys avoid the word "token": the audit scrubber masks token-named fields
                "usage": {"input": used.input_tokens, "output": used.output_tokens},
                "latency_ms": latency,
                "message_count": len(body.messages),
            }
            if self._capture_full():
                details["messages"] = scrub(
                    [{"role": m.role, "content": m.content} for m in body.messages]
                )
                details["output"] = scrub(response.output)
            self._invoked(model, ref, run_id, details)
            self._session.commit()
        finally:
            limiter.release_lease(self._session, self.sc.tenant_id, lease)
        out = s.InvokeOut(
            output=response.output,
            usage=s.UsageOut(input_tokens=used.input_tokens, output_tokens=used.output_tokens),
            latency_ms=latency,
            provider_request_id=response.provider_request_id,
        )
        return RunInvoke(out, response.usage, max_tokens)

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


@dataclass(frozen=True)
class _Requester:
    """Who queued a provider job (`authz.execution.RunRef`): re-derived when the job runs."""

    tenant_id: UUID
    initiated_by_principal_id: UUID
    principal_type: str
    api_key_id: UUID | None


def run_provider_job(
    database: Database, runtime: IntegrationRuntime, tenant_id: UUID, job_id: UUID
) -> str | None:
    """Execute the `model.health_check` or `model.discover` job `job_id` of `tenant_id` (called
    by `shifde-worker`, B-2717).

    Jobs carry no permissions: the requester is re-derived from the database and `model:manage`
    is checked again. Returns None once the result is recorded (now or by an earlier run of the
    same job), else the error code of the refusal or of a failed discovery; nothing is recorded
    then. A failed health check is recorded as `unavailable`."""
    session = database.session()
    db.init_context(session, None)
    try:
        db.set_tenant(session, tenant_id)
        job = repository.get_provider_job(
            session, job_id, tenant_id=tenant_id, kinds=(HEALTH_JOB_KIND, DISCOVER_JOB_KIND)
        )
        if job is None:
            return NotFound.code
        kind, payload = job["kind"], job["payload"]
        provider_id = UUID(payload["provider_id"])
        request_id = f"{JOB_PREFIX[kind]}-{job_id}"
        db.set_request_id(session, request_id)
        recorded = audit.find_details(
            session,
            tenant_id=tenant_id,
            action=JOB_ACTION[kind],
            target_id=provider_id,
            correlation_id=request_id,
            occurred_from=job["created_at"],
        )
        if recorded is not None:
            return None
        try:
            principal = authz.execution_principal(
                session,
                _Requester(
                    tenant_id,
                    UUID(payload["principal_id"]),
                    payload["principal_type"],
                    UUID(payload["api_key_id"]) if payload["api_key_id"] else None,
                ),
            )
            header = tenancy.tenant_header(session, tenant_id)
            if header is None:
                raise authz.ExecutionDenied("the client is not available")
            scope = TenantScope(
                session=session,
                ctx=RequestContext(request_id=request_id, principal=principal, tenant_id=tenant_id),
                principal=principal,
                tenant_id=tenant_id,
                tenant_slug=header[0],
                tenant_name=header[1],
                tenant_status=header[2],
                pdp=authz.new_pdp(session, principal, tenant_id),
            )
            svc = ModelService(scope, runtime)
            if kind == HEALTH_JOB_KIND:
                svc.execute_health_check(provider_id)
            else:
                svc.execute_discovery(provider_id)
        except AppError as exc:
            session.rollback()
            return exc.code
        return None
    finally:
        session.close()


__all__ = [
    "DISCOVER_JOB_KIND",
    "HEALTH_JOB_KIND",
    "INVOCABLE",
    "LIFECYCLE_TRANSITIONS",
    "ModelService",
    "platform_get_shi_access",
    "platform_get_shi_provider",
    "platform_put_shi_provider",
    "platform_set_shi_access",
    "run_provider_job",
]
