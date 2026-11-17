"""Deployment targets and the one-platform-per-organization rule (design sections 3 and 9,
DECISIONS B-2501, lead ruling B-2523).

A target's address is normalized and allow-listed by `adapters.native.addresses` before anything
is written (this module makes no outbound request). The organization's claim on the address comes
from the `shifde.delivery_claim_platform` definer; when another organization holds the address the
caller gets the fixed `409 platform_already_registered`, which names no organization, row,
constraint or key. Archiving the last active target on a claim deletes the claim, so another
organization may register the address afterwards.

Every row is read and written for the caller's resolved client only (`TenantScope.tenant_id`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any  # Any: target rows
from uuid import UUID

from shifde.adapters.native.addresses import (
    AddressRefused,
    address_key,
    normalize_databricks_address,
)
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.core.errors import AppError, Conflict, NotFound, ScopeArchived, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.delivery import repository
from shifde.delivery import schemas as s
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

PLATFORM_ALREADY_REGISTERED = "This platform address is already registered as a deployment target."


class PlatformAlreadyRegistered(AppError):
    """Another organization holds the platform address (one fixed message, B-2501)."""

    status, code, title = 409, "platform_already_registered", "Conflict"

    def __init__(self) -> None:
        super().__init__(PLATFORM_ALREADY_REGISTERED)


class TargetService:
    def __init__(self, scope: TenantScope, host_suffixes: Iterable[str] = ()) -> None:
        self.sc = scope
        self.host_suffixes = tuple(host_suffixes)

    # ================================================================== helpers
    def _ref(self, row: Mapping[str, Any]) -> ResourceRef:
        return ResourceRef(
            self.sc.tenant_id,
            row["workspace_id"],
            row["project_id"],
            resource_type="delivery_target",
            resource_id=row["id"],
        )

    def _get_row(self, target_id: UUID) -> Mapping[str, Any]:
        row = repository.get_target(self.sc.session, target_id, tenant_id=self.sc.tenant_id)
        if row is None or row["tenant_id"] != self.sc.tenant_id:
            raise NotFound("target not found")
        return row

    def _require(self, action: str, row: Mapping[str, Any]) -> None:
        try:
            self.sc.pdp.require(action, self._ref(row), hide_unless="delivery:target:read")
        except NotFound as exc:
            # a target the caller cannot read gets the answer an unknown id gets
            raise NotFound("target not found") from exc

    def _active_scope(self, workspace: str, project: str | None) -> None:
        """Archived organizations, workspaces and projects take no new target (as connections)."""
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")
        if tenancy.get_workspace(self.sc, workspace).status != "active":
            raise ScopeArchived("workspace is archived")
        if project is not None and tenancy.get_project(self.sc, workspace, project).status != (
            "active"
        ):
            raise ScopeArchived("project is archived")

    def _audit(self, action: str, row: Mapping[str, Any]) -> None:
        audit.record(
            self.sc.session,
            self.sc.ctx,
            action=action,
            tenant_id=self.sc.tenant_id,
            target_type="delivery_target",
            target_id=row["id"],
            target_label=f"delivery_target:{row['name']}",
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            details={
                "name": row["name"],
                "environment": row["environment"],
                "workspace_id": str(row["workspace_id"]),
                "project_id": str(row["project_id"]) if row["project_id"] else None,
                "platform_kind": row["platform_kind"],
            },
        )

    def _out(
        self,
        row: Mapping[str, Any],
        slugs: tuple[dict[UUID, str], dict[UUID, str]] | None = None,
    ) -> s.TargetOut:
        ws_map, pr_map = slugs or tenancy.scope_slug_map(
            self.sc, [row["workspace_id"]], [row["project_id"]] if row["project_id"] else []
        )
        return s.TargetOut(
            id=row["id"],
            name=row["name"],
            workspace=ws_map.get(row["workspace_id"]),
            project=pr_map.get(row["project_id"]) if row["project_id"] else None,
            platform_kind=row["platform_kind"],
            address=row["address"],
            environment=row["environment"],
            bindings=s.TargetBindings.model_validate(row["bindings"]),
            credential_ref=row["credential_ref"],
            status=row["status"],
            revision=row["revision"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ================================================================== routes
    def list(
        self,
        *,
        status: str | None,
        workspace: str | None,
        project: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[s.TargetOut]:
        pdp = self.sc.pdp
        flt = pdp.visible_scopes("delivery:target:read")
        if not (flt.tenant_wide or flt.workspace_ids or flt.project_ids):
            pdp.require("delivery:target:read", self.sc.tenant_ref)  # 403 + decision record
        ws_id = pr_id = None
        if project and not workspace:
            raise ValidationFailed("`project` requires `workspace`")
        if workspace:
            ref = tenancy.scope_ref_from_slugs(self.sc, workspace, project)
            ws_id, pr_id = ref.workspace_id, ref.project_id
        rows = repository.list_targets(
            self.sc.session,
            tenant_id=self.sc.tenant_id,
            status=status,
            workspace_id=ws_id,
            project_id=pr_id,
            tenant_wide=flt.tenant_wide,
            visible_workspaces=sorted(flt.workspace_ids),
            visible_projects=sorted(flt.project_ids),
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        slugs = tenancy.scope_slug_map(
            self.sc,
            [r["workspace_id"] for r in page],
            [r["project_id"] for r in page if r["project_id"]],
        )
        return Page(
            items=[self._out(r, slugs) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, target_id: UUID) -> s.TargetOut:
        row = self._get_row(target_id)
        self._require("delivery:target:read", row)
        return self._out(row)

    def register_target(self, body: s.TargetCreate) -> s.TargetOut:
        ref = tenancy.scope_ref_from_slugs(self.sc, body.workspace, body.project)
        self.sc.pdp.require("delivery:target:write", ref)
        self._active_scope(body.workspace, body.project)
        try:
            address = normalize_databricks_address(body.address, self.host_suffixes)
        except AddressRefused as exc:
            raise ValidationFailed(
                str(exc),
                errors=[{"loc": ["body", "address"], "msg": str(exc), "type": "address"}],
            ) from exc
        session = self.sc.session
        # B-2523: the definer answers None, naming nobody, when another organization holds it
        claim_id = repository.claim_platform(session, body.platform_kind, address_key(address))
        if claim_id is None:
            session.rollback()
            raise PlatformAlreadyRegistered
        target_id = uuid7()
        repository.insert_target(
            session,
            {
                "id": target_id,
                "tenant_id": self.sc.tenant_id,
                "workspace_id": ref.workspace_id,
                "project_id": ref.project_id,
                "name": body.name,
                "platform_kind": body.platform_kind,
                "address": address,
                "claim_id": claim_id,
                "environment": body.environment,
                "bindings": body.bindings.model_dump(),
                "credential_ref": body.credential_ref,
                "created_by": self.sc.principal.principal_id,
            },
        )
        row = self._get_row(target_id)
        self._audit("delivery.target.registered", row)
        session.commit()
        return self._out(row)

    def archive_target(self, target_id: UUID) -> s.TargetOut:
        row = self._get_row(target_id)
        self._require("delivery:target:write", row)
        if row["status"] != "active":
            raise Conflict("the target is already archived")
        session = self.sc.session
        claim_id = row["claim_id"]
        # the definer's advisory lock: a register reusing this claim and this archive serialize
        repository.lock_claim(session, claim_id, tenant_id=self.sc.tenant_id)
        archived = repository.archive_target(
            session, target_id, claim_id, tenant_id=self.sc.tenant_id
        )
        if archived is None:  # archived by a concurrent request after the read
            session.rollback()
            raise Conflict("the target is already archived")
        if not repository.claim_in_use(session, claim_id, tenant_id=self.sc.tenant_id):
            repository.delete_claim(session, claim_id, tenant_id=self.sc.tenant_id)
        self._audit("delivery.target.archived", archived)
        session.commit()
        return self._out(archived)
