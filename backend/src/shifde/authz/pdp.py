"""Policy Decision Point (SECURITY_MODEL section 3.2).

Default deny. A permission is granted iff the principal (a user, or a service account) holds a
non-expired role binding -- directly or through an active group -- at a scope covering the resource
(tenant > workspace > project > resource), whose role contains the permission. API-key principals
are additionally masked: effective permission = grants INTERSECT key scopes INTERSECT resource
scope. ``platform:admin`` requires ``users.is_platform_admin`` and is never granted to a key.

Grants are loaded lazily, once per PDP instance (i.e. once per request); decisions are never cached
across requests. Denials on resources the caller cannot see surface as 404 (no existence oracle);
denials on visible resources surface as 403.

``require`` records every denial and every privileged allow in the session (``pdp_records``); a
request-end hook persists them to `policy_decisions` in a fresh transaction so they survive the
request rollback that a denial usually causes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.authz import repository
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal
from shifde.core.errors import Forbidden, NotFound
from shifde.core.hooks import add_close_hook
from shifde.core.ids import uuid7

PLATFORM_ONLY_ACTIONS = frozenset({"platform:admin"})
RECORDS_KEY = "pdp_records"
HOOK_FLAG = "pdp_hook_registered"


@dataclass(frozen=True)
class ResourceRef:
    """A resource's ancestry chain: tenant, optional workspace/project, optional resource id."""

    tenant_id: UUID
    workspace_id: UUID | None = None
    project_id: UUID | None = None
    resource_type: str | None = None
    resource_id: UUID | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    matched_binding_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class DecisionRecord:
    tenant_id: UUID | None
    principal_id: UUID
    principal_type: str
    credential: str
    action: str
    resource_type: str | None
    resource_id: str | None
    decision: str
    reason: str
    matched_binding_ids: tuple[UUID, ...]
    request_id: str | None


@dataclass(frozen=True)
class ScopeFilter:
    """Where a principal holds an action: everywhere in the tenant, or in specific scopes."""

    tenant_wide: bool = False
    workspace_ids: frozenset[UUID] = field(default_factory=frozenset)
    project_ids: frozenset[UUID] = field(default_factory=frozenset)

    def covers_workspace(self, workspace_id: UUID) -> bool:
        return self.tenant_wide or workspace_id in self.workspace_ids

    def covers_project(self, workspace_id: UUID, project_id: UUID) -> bool:
        return self.covers_workspace(workspace_id) or project_id in self.project_ids


def _flush_decisions(session: Session) -> None:
    """Close hook: persist `pdp_records` as policy_decisions rows (fresh transaction)."""
    records: list[DecisionRecord] = session.info.pop(RECORDS_KEY, [])
    session.info.pop(HOOK_FLAG, None)
    by_tenant: dict[UUID, list[DecisionRecord]] = {}
    for rec in records:
        if rec.tenant_id is not None:  # platform-only decisions have no tenant to attribute to
            by_tenant.setdefault(rec.tenant_id, []).append(rec)
    for tenant_id, recs in by_tenant.items():
        db.set_tenant(session, tenant_id)
        repository.insert_decisions(
            session,
            [
                {
                    "id": uuid7(),
                    "tenant_id": tenant_id,
                    "at": utcnow(),
                    "principal_id": r.principal_id,
                    "principal_type": r.principal_type,
                    "action": r.action,
                    "resource_type": r.resource_type,
                    "resource_id": r.resource_id,
                    "decision": r.decision,
                    "reason_codes": [r.reason],
                    "matched_binding_ids": list(r.matched_binding_ids),
                    "trace_id": r.request_id,
                }
                for r in recs
            ],
        )
        session.commit()
    session.info[RECORDS_KEY] = records  # kept for later hooks (denial audit); cleared by caller


class PDP:
    def __init__(self, session: Session, principal: Principal, tenant_id: UUID | None) -> None:
        self._session = session
        self._principal = principal
        self._tenant_id = tenant_id
        self._grants: list[repository.GrantRow] | None = None
        self._privileged: frozenset[str] | None = None

    # ------------------------------------------------------------------ grants
    def _load(self) -> list[repository.GrantRow]:
        if self._grants is None:
            if self._tenant_id is None:
                self._grants = []
            else:
                grants = repository.load_grants(
                    self._session,
                    tenant_id=self._tenant_id,
                    principal_type=self._principal.principal_type,
                    principal_id=self._principal.principal_id,
                    now=utcnow(),
                )
                scopes = self._principal.key_scopes
                if scopes is not None:  # API-key permission mask
                    grants = [g for g in grants if g.permission_key in scopes]
                self._grants = grants
        return self._grants

    def _is_platform_admin(self) -> bool:
        return self._principal.is_platform_admin and self._principal.credential != "api_key"

    def _in_resource_scope(self, resource: ResourceRef) -> bool:
        mask = self._principal.resource_scope
        if mask is None:
            return True
        return (
            resource.workspace_id is not None and resource.workspace_id in mask.workspace_ids
        ) or (resource.project_id is not None and resource.project_id in mask.project_ids)

    @staticmethod
    def _covers(g: repository.GrantRow, resource: ResourceRef) -> bool:
        if g.scope_type == "tenant":
            return g.scope_id == resource.tenant_id
        if g.scope_type == "workspace":
            return resource.workspace_id is not None and g.scope_id == resource.workspace_id
        if g.scope_type == "project":
            return resource.project_id is not None and g.scope_id == resource.project_id
        return (  # resource-level binding
            resource.resource_id is not None
            and g.scope_id == resource.resource_id
            and g.resource_type == resource.resource_type
        )

    # ------------------------------------------------------------------ decisions
    def authorize(self, action: str, resource: ResourceRef | None = None) -> Decision:
        if action in PLATFORM_ONLY_ACTIONS:
            if self._is_platform_admin():
                return Decision(True, "platform_admin")
            return Decision(False, "not_platform_admin")
        if resource is None or self._tenant_id is None or resource.tenant_id != self._tenant_id:
            return Decision(False, "cross_tenant")
        scopes = self._principal.key_scopes
        if scopes is not None and action not in scopes:
            return Decision(False, "key_scope")
        if not self._in_resource_scope(resource):
            return Decision(False, "resource_scope")
        matched = [
            g.binding_id
            for g in self._load()
            if g.permission_key == action and self._covers(g, resource)
        ]
        if matched:
            return Decision(True, "binding", tuple(dict.fromkeys(matched)))
        return Decision(False, "no_binding")

    def _is_privileged_action(self, action: str) -> bool:
        if self._privileged is None:
            self._privileged = repository.privileged_keys(self._session)
        return action in self._privileged

    def _record(self, action: str, resource: ResourceRef | None, decision: Decision) -> None:
        p = self._principal
        records: list[DecisionRecord] = self._session.info.setdefault(RECORDS_KEY, [])
        records.append(
            DecisionRecord(
                tenant_id=self._tenant_id,
                principal_id=p.principal_id,
                principal_type=p.principal_type,
                credential=p.credential,
                action=action,
                resource_type=resource.resource_type if resource else None,
                resource_id=str(
                    resource.resource_id
                    or resource.project_id
                    or resource.workspace_id
                    or resource.tenant_id
                )
                if resource
                else None,
                decision="allow" if decision.allowed else "deny",
                reason=decision.reason,
                matched_binding_ids=decision.matched_binding_ids,
                request_id=db.get_context(self._session).request_id,
            )
        )
        if not self._session.info.get(HOOK_FLAG):
            self._session.info[HOOK_FLAG] = True
            add_close_hook(self._session, _flush_decisions)

    def require(
        self,
        action: str,
        resource: ResourceRef | None = None,
        *,
        hide_unless: str | None = None,
    ) -> Decision:
        """Raise unless allowed. If ``hide_unless`` is given and the principal lacks *that*
        (visibility) permission on the resource, raise NotFound instead of Forbidden."""
        decision = self.authorize(action, resource)
        if decision.allowed:
            if self._is_privileged_action(action) or action in PLATFORM_ONLY_ACTIONS:
                self._record(action, resource, decision)
            return decision
        self._record(action, resource, decision)
        if hide_unless is not None and not self.authorize(hide_unless, resource).allowed:
            raise NotFound("resource not found")
        raise Forbidden(f"missing permission: {action}")

    # ------------------------------------------------------------------ queries
    def effective_permissions(self, resource: ResourceRef) -> set[str]:
        """Every permission the principal holds at `resource` (for me/permissions and guards)."""
        if self._tenant_id is None or resource.tenant_id != self._tenant_id:
            return set()
        perms: set[str] = set()
        if self._in_resource_scope(resource):
            perms = {g.permission_key for g in self._load() if self._covers(g, resource)}
        if self._is_platform_admin():
            perms.add("platform:admin")
        return perms

    def missing_permissions(
        self, actions: set[str] | frozenset[str], resource: ResourceRef
    ) -> set[str]:
        """Which of `actions` the principal does NOT hold at `resource` (escalation guard)."""
        held = self.effective_permissions(resource)
        return {a for a in actions if a not in held}

    def visible_scopes(self, action: str) -> ScopeFilter:
        """Scopes at which the principal holds `action` (for list-query predicates). For a
        resource-scoped API key the result is restricted to the key's workspaces/projects."""
        tenant_wide = False
        workspaces: set[UUID] = set()
        projects: set[UUID] = set()
        scopes = self._principal.key_scopes
        if scopes is not None and action not in scopes:
            return ScopeFilter()
        for g in self._load():
            if g.permission_key != action:
                continue
            if g.scope_type == "tenant":
                tenant_wide = True
            elif g.scope_type == "workspace":
                workspaces.add(g.scope_id)
            elif g.scope_type == "project":
                projects.add(g.scope_id)
        mask = self._principal.resource_scope
        if mask is not None:
            if tenant_wide:  # the key narrows a tenant-wide grant to its listed ids
                return ScopeFilter(False, mask.workspace_ids, mask.project_ids)
            return ScopeFilter(
                False,
                frozenset(workspaces) & mask.workspace_ids,
                frozenset(projects) & mask.project_ids,
            )
        return ScopeFilter(tenant_wide, frozenset(workspaces), frozenset(projects))

    def ancestor_workspace_ids(self) -> frozenset[UUID]:
        """Workspaces that contain something the principal holds *any* grant on. The principal
        may read those workspaces (metadata only) so selectors can show the path to its grants;
        nothing else is implied (P1 gap fix)."""
        if self._tenant_id is None:
            return frozenset()
        grants = self._load()
        workspaces = {g.scope_id for g in grants if g.scope_type == "workspace"}
        project_ids = {g.scope_id for g in grants if g.scope_type == "project"}
        workspaces |= repository.workspace_ids_of_projects(
            self._session, self._tenant_id, project_ids
        )
        mask = self._principal.resource_scope
        if mask is not None:
            allowed = set(mask.workspace_ids) | repository.workspace_ids_of_projects(
                self._session, self._tenant_id, mask.project_ids
            )
            workspaces &= allowed
        return frozenset(workspaces)
