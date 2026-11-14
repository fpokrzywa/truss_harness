"""Vendor catalog + adapter registry use cases.

Capability maturity is *derived* (adapter declaration + recorded conformance evidence); there is no
code path that stores or accepts a maturity value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any  # Any: vendor rows / JSON documents
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters import evidence
from shifde.adapters.base import AdapterDescriptor
from shifde.adapters.maturity import CapabilityMaturity, adapter_capabilities, capability_maturity
from shifde.adapters.registry import AdapterRegistry
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import PDP, ResourceRef
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.errors import Conflict, Forbidden, NotFound, ScopeArchived, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.tenancy.scope import TenantScope
from shifde.vendors import repository
from shifde.vendors import schemas as s


def _evidence(m: CapabilityMaturity) -> s.EvidenceOut | None:
    ev = m.evidence
    return (
        s.EvidenceOut(run_id=ev.run_id, finished_at=ev.finished_at, suite_version=ev.suite_version)
        if ev
        else None
    )


def _cap_out(m: CapabilityMaturity) -> s.CapabilityOut:
    return s.CapabilityOut(capability=m.capability, maturity=m.maturity, evidence=_evidence(m))


CHECK_FIELDS = ("name", "status", "capabilities")


def client_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a client may see of a recorded check: its name, outcome and capabilities.

    `detail` is free text the suite produced (an error tail, a returned note); against a
    sample database that is a client's, it can name that client's tables or carry its SQL
    (carry_forward, adapters review 1; DECISIONS B-1302). Only the allowlisted keys leave the
    service; the owner CLI still reads the stored row.
    """
    return [{k: c[k] for k in CHECK_FIELDS if k in c} for c in checks]


def adapter_out(d: AdapterDescriptor, latest: evidence.ConformanceRun | None) -> s.AdapterOut:
    return s.AdapterOut(
        key=d.key,
        kind=d.kind,
        version=d.version,
        display_name=d.display_name,
        connection_type=d.connection_type,
        capabilities=[_cap_out(m) for m in adapter_capabilities(d, latest)],
        config_schema=d.config_schema,
        secret_fields=list(d.secret_fields),
        auth_methods=list(d.auth_methods),
    )


def list_adapters(session: Session, registry: AdapterRegistry) -> Page[s.AdapterOut]:
    latest = evidence.latest_runs(session)
    return Page(items=[adapter_out(d, latest.get(d.key)) for d in registry.descriptors()])


def list_conformance_runs(
    session: Session, registry: AdapterRegistry, key: str
) -> Page[s.ConformanceRunOut]:
    if registry.descriptor(key) is None:
        raise NotFound("adapter not found")
    return Page(
        items=[
            s.ConformanceRunOut(
                id=r.id,
                adapter_key=r.adapter_key,
                adapter_version=r.adapter_version,
                suite_version=r.suite_version,
                started_at=r.started_at,
                finished_at=r.finished_at,
                outcome=r.outcome,
                validated_capabilities=list(r.validated_capabilities),
                checks=client_checks(r.checks),
                environment=r.environment,
            )
            for r in evidence.list_runs(session, key)
        ]
    )


@dataclass
class VendorService:
    session: Session
    ctx: RequestContext
    principal: Principal
    pdp: PDP
    registry: AdapterRegistry
    tenant_id: UUID | None  # None = platform (global catalog) context
    tenant_status: str = "active"

    @classmethod
    def for_tenant(cls, scope: TenantScope, registry: AdapterRegistry) -> VendorService:
        return cls(
            scope.session,
            scope.ctx,
            scope.principal,
            scope.pdp,
            registry,
            scope.tenant_id,
            scope.tenant_status,
        )

    @classmethod
    def for_platform(
        cls, session: Session, ctx: RequestContext, principal: Principal, registry: AdapterRegistry
    ) -> VendorService:
        return cls(session, ctx, principal, authz.new_pdp(session, principal, None), registry, None)

    # ------------------------------------------------------------------ helpers
    def _ref(self) -> ResourceRef | None:
        return ResourceRef(self.tenant_id) if self.tenant_id else None

    def _require(self, action: str) -> None:
        if self.tenant_id is None:
            self.pdp.require("platform:admin")
        else:
            self.pdp.require(action, ResourceRef(self.tenant_id))
            if action.endswith(":manage") and self.tenant_status != "active":
                raise ScopeArchived("organization is archived")

    def _out(self, row: Mapping[str, Any]) -> s.VendorOut:
        return s.VendorOut(
            id=row["id"],
            tenant_id=row["tenant_id"],
            is_global=row["tenant_id"] is None,
            scope="global" if row["tenant_id"] is None else "tenant",
            key=row["key"],
            name=row["name"],
            category=row["category"],
            description=row["description"],
            version=row["version"],
            status=row["status"],
            configuration_requirements=list(row["configuration_requirements"] or []),
            adapter_keys=list(row["adapter_keys"] or []),
            row_version=row["row_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _get_row(self, vendor_id: UUID) -> Mapping[str, Any]:
        row = repository.get_vendor(self.session, vendor_id)
        if row is None or (self.tenant_id is None and row["tenant_id"] is not None):
            raise NotFound("vendor not found")
        return row

    def _check_adapters(self, keys: list[str]) -> None:
        unknown = [k for k in keys if self.registry.descriptor(k) is None]
        if unknown:
            raise ValidationFailed(f"unknown adapter key(s): {', '.join(sorted(unknown))}")

    def _audit(self, action: str, row: Mapping[str, Any], details: dict[str, object]) -> None:
        def record() -> None:
            audit.record(
                self.session,
                self.ctx,
                action=action,
                tenant_id=self.tenant_id,
                target_type="vendor",
                target_id=row["id"],
                target_label=f"vendor:{row['key']}",
                details=details,
            )

        if self.tenant_id is None:  # the global catalog: an installation-level event (B-1304)
            with db.platform_admin_context(self.session):
                record()
        else:
            record()

    def _commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise Conflict("a vendor with this key already exists") from exc

    # ------------------------------------------------------------------ use cases
    def list(
        self, *, category: str | None, status: str | None, limit: int, cursor: str | None
    ) -> Page[s.VendorOut]:
        self._require("vendor:read")
        rows = repository.list_vendors(
            self.session,
            category=category,
            status=status,
            only_global=self.tenant_id is None,
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[self._out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, vendor_id: UUID) -> s.VendorOut:
        self._require("vendor:read")
        return self._out(self._get_row(vendor_id))

    def create(self, body: s.VendorCreate) -> s.VendorOut:
        self._require("vendor:manage")
        self._check_adapters(body.adapter_keys)
        vendor_id = uuid7()
        values = {
            "id": vendor_id,
            "tenant_id": self.tenant_id,
            "key": body.key,
            "name": body.name,
            "category": body.category,
            "description": body.description,
            "version": body.version,
            "status": "active",
            "configuration_requirements": body.configuration_requirements,
            "adapter_keys": body.adapter_keys,
            "created_by": self.principal.principal_id,
        }
        try:
            if self.tenant_id is None:
                with db.platform_admin_context(self.session):
                    repository.insert_vendor(self.session, values)
                    self.session.flush()
            else:
                repository.insert_vendor(self.session, values)
                self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise Conflict("a vendor with this key already exists") from exc
        return self._finish_create(vendor_id)

    def _finish_create(self, vendor_id: UUID) -> s.VendorOut:
        row = self._get_row(vendor_id)
        self._audit("vendor.created", row, {"key": row["key"], "category": row["category"]})
        self._commit()
        return self._out(row)

    def update(self, vendor_id: UUID, body: s.VendorPatch) -> s.VendorOut:
        self._require("vendor:manage")
        row = self._get_row(vendor_id)
        if row["tenant_id"] is not None and row["tenant_id"] != self.tenant_id:
            raise NotFound("vendor not found")
        if row["tenant_id"] is None and self.tenant_id is not None:
            raise Forbidden("global catalog vendors are read-only for organizations")
        sent = body.model_fields_set - {"row_version"}
        if not sent:
            raise ValidationFailed("no updatable fields supplied")
        if body.adapter_keys is not None:
            self._check_adapters(body.adapter_keys)
        values: dict[str, object] = {
            k: getattr(body, k) for k in sent if getattr(body, k) is not None or k == "description"
        }
        if self.tenant_id is None:
            with db.platform_admin_context(self.session):
                updated = repository.update_vendor(
                    self.session, vendor_id, body.row_version, values
                )
        else:
            updated = repository.update_vendor(self.session, vendor_id, body.row_version, values)
        if updated is None:
            raise Conflict("row_version mismatch: the vendor was modified; reload and retry")
        self._audit("vendor.updated", updated, {"changed": sorted(sent)})
        self._commit()
        return self._out(updated)

    def set_status(self, vendor_id: UUID, body: s.VendorStatusChange) -> s.VendorOut:
        return self.update(
            vendor_id, s.VendorPatch(status=body.status, row_version=body.row_version)
        )

    def capabilities(self, vendor_id: UUID) -> Page[s.VendorCapabilityOut]:
        self._require("vendor:read")
        row = self._get_row(vendor_id)
        latest = evidence.latest_runs(self.session)
        declared: list[dict[str, Any]] = list(row["capabilities"] or [])
        seen = {(d["capability"], d.get("adapter_key")) for d in declared}
        for adapter_key in row["adapter_keys"] or []:  # tenant vendors derive from their adapters
            adapter_desc = self.registry.descriptor(adapter_key)
            if adapter_desc is not None:
                for capability in sorted(adapter_desc.capabilities, key=str):
                    if (str(capability), adapter_key) not in seen:
                        declared.append({"capability": str(capability), "adapter_key": adapter_key})
        items: list[s.VendorCapabilityOut] = []
        for entry in declared:
            cap = str(entry["capability"])
            key: str | None = entry.get("adapter_key")
            desc = self.registry.descriptor(key) if key else None
            if key is None or desc is None or cap not in {str(c) for c in desc.capabilities}:
                m = CapabilityMaturity(cap, "planned")
            else:
                m = capability_maturity(desc, cap, latest.get(key))
            items.append(
                s.VendorCapabilityOut(
                    capability=cap,
                    adapter_key=key if desc is not None else None,
                    maturity=m.maturity,
                    evidence=_evidence(m),
                )
            )
        return Page(items=items)


def get_visible_vendor_row(session: Session, tenant_id: UUID, vendor_id: UUID) -> Mapping[str, Any]:
    """Cross-module lookup: a vendor visible to the tenant (global or its own), else NotFound."""
    row = repository.get_vendor(session, vendor_id)
    if row is None or row["tenant_id"] not in (None, tenant_id):
        raise NotFound("vendor not found")
    return row


def require_active_vendor(session: Session, tenant_id: UUID, vendor_id: UUID) -> Mapping[str, Any]:
    """Like `get_visible_vendor_row`, but a vendor that is not active is refused (422). Used when
    a NEW object names a vendor; objects that already reference it keep working (the vendor status
    is an approval gate for new use, not a kill switch). Adapter keys naming no vendor are not
    gated."""
    row = get_visible_vendor_row(session, tenant_id, vendor_id)
    if row["status"] != "active":
        raise ValidationFailed(
            f"vendor {row['key']!r} is {row['status']}; only active vendors can be used by "
            "new connections or model providers"
        )
    return row


__all__ = [
    "VendorService",
    "adapter_out",
    "get_visible_vendor_row",
    "list_adapters",
    "require_active_vendor",
    "utcnow",
]
