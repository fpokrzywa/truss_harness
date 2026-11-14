"""Authz use cases other modules call (never their repositories directly).

Data operations only: callers (the admin services) own PDP checks, audit and the transaction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from shifde.authz import repository
from shifde.authz.execution import ExecutionDenied, RunRef, execution_principal
from shifde.authz.models import Group, Role
from shifde.authz.pdp import (
    PDP,
    PLATFORM_ONLY_ACTIONS,
    Decision,
    DecisionRecord,
    GroupMembership,
    ResourceRef,
    ScopeFilter,
    platform_group_membership,
)
from shifde.authz.repository import BindingRow, BoundGroup, GrantRow, MemberRef, PermissionRow
from shifde.core.clock import utcnow
from shifde.core.context import Principal
from shifde.core.errors import Forbidden, ValidationFailed
from shifde.core.ids import uuid7

__all__ = [
    "PDP",
    "NAMING_AN_OWNER_REFUSED",
    "OWNER_ROLE_KEY",
    "PLATFORM_ONLY_ACTIONS",
    "PLATFORM_ONLY_ROLE_KEYS",
    "BindingRow",
    "BoundGroup",
    "Decision",
    "DecisionRecord",
    "ExecutionDenied",
    "GrantRow",
    "Group",
    "GroupMembership",
    "MemberRef",
    "PermissionRow",
    "ResourceRef",
    "Role",
    "RunRef",
    "ScopeFilter",
    "add_group_members",
    "bindings_of_subject",
    "bound_groups",
    "delete_binding",
    "delete_bindings_of_subject",
    "delete_role_permissions",
    "execution_principal",
    "existing_permission_keys",
    "get_binding",
    "get_group",
    "get_group_by_slug",
    "get_idp_group",
    "get_role",
    "grant_role",
    "group_members",
    "groups_of_users",
    "idp_synced_group_ids_of_user",
    "insert_binding",
    "insert_decisions",
    "insert_group",
    "insert_role",
    "insert_role_permissions",
    "is_platform_only_role",
    "list_bindings",
    "list_groups",
    "list_permissions",
    "list_roles",
    "may_name_owner",
    "member_counts",
    "member_user_ids",
    "new_pdp",
    "permissions_by_role",
    "platform_group_membership",
    "privileged_keys",
    "remove_group_members",
    "role_key_exists",
    "subject_ids_covering",
    "sync_idp_groups",
    "update_group",
    "update_role",
]


# System role templates that are never bound inside a client (B-1010).
PLATFORM_ONLY_ROLE_KEYS = frozenset({"platform_administrator"})
# Bound directly to a person at organization scope, this template makes the client's owner.
OWNER_ROLE_KEY = "tenant_administrator"
NAMING_AN_OWNER_REFUSED = "only the owner or the platform administrator may name an owner"


def is_platform_only_role(session: Session, role_id: UUID) -> bool:
    """True when `role_id` is a system template that is never bound inside a client."""
    return any(repository.system_role_id(session, k) == role_id for k in PLATFORM_ONLY_ROLE_KEYS)


def new_pdp(
    session: Session,
    principal: Principal,
    tenant_id: UUID | None,
    *,
    group_membership: GroupMembership = platform_group_membership,
) -> PDP:
    return PDP(session, principal, tenant_id, group_membership=group_membership)


def grant_role(
    session: Session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    role_key: str,
    scope_type: str,
    scope_id: UUID,
    created_by: UUID | None,
) -> None:
    """Bind a system role template to a user at a scope (caller has already authorized). The
    `platform_administrator` template is never bound in a client (authz review 2 item 5,
    DECISIONS B-1010; `check_role_binding` refuses it as well).

    `tenant_administrator` at organization scope names the client's owner: only the
    platform-administrator context or that client's owner may bind it, never `role:manage` alone
    (tenancy isolation review 2 item 2, B-1016; the trigger checks the same rule)."""
    if role_key in PLATFORM_ONLY_ROLE_KEYS:
        raise ValidationFailed(f"unknown role: {role_key}")
    role_id = repository.system_role_id(session, role_key)
    if role_id is None:
        raise ValidationFailed(f"unknown role: {role_key}")
    if (
        role_key == OWNER_ROLE_KEY
        and scope_type == "tenant"
        and not repository.may_name_owner(session, tenant_id)
    ):
        raise Forbidden(NAMING_AN_OWNER_REFUSED)
    repository.insert_binding(
        session,
        {
            "id": uuid7(),
            "tenant_id": tenant_id,
            "subject_type": "user",
            "subject_id": user_id,
            "role_id": role_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "created_by": created_by,
            "created_at": utcnow(),
        },
    )


def subject_ids_covering(
    session: Session,
    *,
    tenant_id: UUID,
    principal: Principal,
    resource: ResourceRef,
    group_membership: GroupMembership = platform_group_membership,
) -> tuple[set[UUID], set[UUID]]:
    """(role ids, group ids) the principal holds that apply to `resource`: a role counts when a
    binding of it covers the resource's scope (tenant, workspace, project or the resource);
    groups are the principal's active group memberships. Used by ontology access policies."""
    groups: set[UUID] = (
        set(group_membership(session, tenant_id=tenant_id, user_id=principal.principal_id))
        if principal.principal_type == "user"
        else set()
    )
    roles: set[UUID] = set()
    for role_id, scope_type, scope_id, resource_type in repository.bound_roles(
        session,
        tenant_id=tenant_id,
        principal_type=principal.principal_type,
        principal_id=principal.principal_id,
        group_ids=groups,
        now=utcnow(),
    ):
        covered = (
            (scope_type == "tenant" and scope_id == resource.tenant_id)
            or (scope_type == "workspace" and scope_id == resource.workspace_id)
            or (scope_type == "project" and scope_id == resource.project_id)
            or (
                scope_type == "resource"
                and scope_id == resource.resource_id
                and resource_type == resource.resource_type
            )
        )
        if covered:
            roles.add(role_id)
    return roles, groups


# Thin pass-throughs: the repository stays private to this module.
list_permissions = repository.list_permissions
existing_permission_keys = repository.existing_permission_keys
privileged_keys = repository.privileged_keys
list_roles = repository.list_roles
get_role = repository.get_role
role_key_exists = repository.role_key_exists
permissions_by_role = repository.permissions_by_role
insert_role = repository.insert_role
insert_role_permissions = repository.insert_role_permissions
delete_role_permissions = repository.delete_role_permissions
update_role = repository.update_role
list_bindings = repository.list_bindings
get_binding = repository.get_binding
delete_binding = repository.delete_binding
bindings_of_subject = repository.bindings_of_subject
delete_bindings_of_subject = repository.delete_bindings_of_subject
insert_binding = repository.insert_binding
insert_decisions = repository.insert_decisions
insert_group = repository.insert_group
get_group = repository.get_group
get_group_by_slug = repository.get_group_by_slug
get_idp_group = repository.get_idp_group
list_groups = repository.list_groups
update_group = repository.update_group
group_members = repository.group_members
member_counts = repository.member_counts
groups_of_users = repository.groups_of_users
bound_groups = repository.bound_groups
sync_idp_groups = repository.sync_idp_groups
member_user_ids = repository.member_user_ids
add_group_members = repository.add_group_members
remove_group_members = repository.remove_group_members
idp_synced_group_ids_of_user = repository.idp_synced_group_ids_of_user
may_name_owner = repository.may_name_owner
