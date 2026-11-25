"""Policy Decision Point (SECURITY_MODEL section 3.2).

Default deny. A permission is granted iff the principal (a user, or a service account) holds a
non-expired role binding -- directly or through an active group -- at a scope covering the resource
(tenant > workspace > project > resource), whose role contains the permission. API-key principals
are additionally masked: effective permission = grants INTERSECT key scopes INTERSECT resource
scope. ``platform:admin`` (every key in ``PLATFORM_ONLY_ACTIONS``) requires
``users.is_platform_admin`` and is never granted to a key, nor by any role binding: grants loaded
through bindings drop platform-only permissions, so every query below agrees with ``authorize``,
and ``visible_scopes`` of a platform-only action is empty for everyone (it is not a client scope;
DECISIONS B-406). The database also refuses a platform-only permission in a client role
(``authz_0002``).

Grants are loaded lazily, once per PDP instance (i.e. once per request); decisions are never cached
across requests. Denials on resources the caller cannot see surface as 404 (no existence oracle);
denials on visible resources surface as 403.

``require`` records every denial and every privileged allow in the session (``pdp_records``); a
request-end hook persists them to `policy_decisions` in a fresh transaction so they survive the
request rollback that a denial usually causes.

Groups (design 5.3, DECISIONS B-403): which groups a user belongs to is answered by a
``GroupMembership`` (default: ``platform_group_membership``, SHI-wide groups). The PDP
then counts only role bindings stored in the client it decides for whose subject is one of those
groups, so membership alone grants nothing and a group with no binding in a client gives no access
there. Identity part 3b (B-929) made groups platform-level and swapped the answer without
changing the decision logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.authz import repository
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, ResourceScope
from shifde.core.errors import Forbidden, NotFound
from shifde.core.hooks import add_close_hook
from shifde.core.ids import uuid7

PLATFORM_ONLY_ACTIONS = frozenset({"platform:admin"})
RECORDS_KEY = "pdp_records"
HOOK_FLAG = "pdp_hook_registered"


class GroupMembership(Protocol):
    """Ids of the active groups `user_id` belongs to that may hold role bindings in `tenant_id`.

    Implementations return group ids only; what a group may do in a client is decided solely by
    the role bindings stored in that client (``role_bindings.tenant_id``, also enforced by RLS)."""

    def __call__(self, session: Session, *, tenant_id: UUID, user_id: UUID) -> frozenset[UUID]: ...


def platform_group_membership(
    session: Session, *, tenant_id: UUID, user_id: UUID
) -> frozenset[UUID]:
    """SHI-wide groups (design 5.3, identity_0005, B-929): the person's active platform groups
    that hold a role binding in `tenant_id`, the database's current client (else empty)."""
    return frozenset(repository.platform_group_ids(session, tenant_id, user_id))


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
    def __init__(
        self,
        session: Session,
        principal: Principal,
        tenant_id: UUID | None,
        *,
        group_membership: GroupMembership = platform_group_membership,
    ) -> None:
        self._session = session
        self._principal = principal
        self._tenant_id = tenant_id
        self._group_membership = group_membership
        self._grants: list[repository.GrantRow] | None = None
        self._privileged: frozenset[str] | None = None

    @property
    def tenant_id(self) -> UUID | None:
        """The client this PDP decides in (None: no client)."""
        return self._tenant_id

    # ------------------------------------------------------------------ grants
    def _load(self) -> list[repository.GrantRow]:
        if self._grants is None:
            if self._tenant_id is None:
                self._grants = []
            else:
                p = self._principal
                groups = (
                    self._group_membership(
                        self._session, tenant_id=self._tenant_id, user_id=p.principal_id
                    )
                    if p.principal_type == "user"
                    else frozenset()
                )
                grants = repository.load_grants(
                    self._session,
                    tenant_id=self._tenant_id,
                    principal_type=p.principal_type,
                    principal_id=p.principal_id,
                    group_ids=groups,
                    now=utcnow(),
                )
                # Platform-only actions come only from the admin flag, never from a role: a
                # client role (or a binding to the shared platform_administrator template) must
                # not surface them through effective_permissions / missing_permissions /
                # visible_scopes / ancestor_workspace_ids (DECISIONS B-406).
                grants = [g for g in grants if g.permission_key not in PLATFORM_ONLY_ACTIONS]
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

    def granted_workspace_ids(self) -> frozenset[UUID]:
        """Workspaces that contain something the principal holds any workspace- or project-scope
        grant on, within the key's mask."""
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
            workspaces &= self._mask_workspace_ids(self._tenant_id, mask)
        return frozenset(workspaces)

    def _mask_workspace_ids(self, tenant_id: UUID, mask: ResourceScope) -> set[UUID]:
        """The mask's workspaces and those of the mask's projects, in this client only: an id of
        another client or of nothing gives no workspace (DECISIONS B-4094)."""
        return repository.workspace_ids_of_client(
            self._session, tenant_id, mask.workspace_ids
        ) | repository.workspace_ids_of_projects(self._session, tenant_id, mask.project_ids)

    def ancestor_workspace_ids(self) -> frozenset[UUID]:
        """Workspaces that contain something the principal holds *any* grant on. The principal
        may read those workspaces (metadata only) so selectors can show the path to its grants;
        nothing else is implied (P1 gap fix).

        A masked principal (a key with a resource scope) that holds `workspace:read` at
        organization level also gets the mask's workspaces and those of the mask's projects, so it
        reads its own project by slug. It must be `workspace:read`, not any grant: a key never
        reads a workspace its owner cannot read (DECISIONS B-4040)."""
        workspaces = self.granted_workspace_ids()
        mask = self._principal.resource_scope
        if (
            self._tenant_id is not None
            and mask is not None
            and any(
                g.permission_key == "workspace:read" and g.scope_type == "tenant"
                for g in self._load()
            )
        ):
            workspaces |= self._mask_workspace_ids(self._tenant_id, mask)
        return workspaces
