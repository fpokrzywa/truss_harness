"""Service accounts and API keys (SECURITY_MODEL 2.4, contract section 3).

Escalation rules enforced here: a key's scopes must be a subset of what the *creator* holds (at the
key's resource scope, if it has one); privileged scopes therefore need a creator that holds them;
`platform:admin` can never be a key scope; user keys can only be minted by the user for itself.
Rotation re-checks the same rule, so rotating cannot launder a permission the rotator lacks.

Ported from SETH 3ba0e06 `admin/credentials.py` (admin part 2). Changes (DECISIONS B-1601..):

* Every route's client comes from `tenancy.dependencies.resolve_org` (B-1019): a key or service
  account acts only in its own client. Rows are read with the client's id and row-level security
  (`identity.get_service_account`, `get_api_key`); the id-only mutators (`revoke_api_key`,
  `shorten_api_key_expiry`) run only on a row loaded in the caller's client (carry_forward 87).
* `apikeys.mint` gets the caller's own principal id as `created_by` (B-921); every refusal of the
  mint, and every name clash on a rename, is one generic answer (review 1 item 3).
* Every `PLATFORM_ONLY_ACTIONS` key is refused as a key scope; re-enabling a service account
  needs the caller to hold what its bindings grant (`iam.guard_bindings`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from shifde.admin import iam
from shifde.admin import schemas as s
from shifde.authz import service as authz
from shifde.core.clock import utcnow
from shifde.core.context import ResourceScope
from shifde.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.core.settings import Settings
from shifde.identity import apikeys
from shifde.identity import service as identity
from shifde.identity.service import ApiKey, ServiceAccount
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

SA_NAME_TAKEN = "a service account with this name already exists"  # = identity's create answer


# ------------------------------------------------------------------ service accounts
def _sa_out(sa: ServiceAccount) -> s.ServiceAccountOut:
    return s.ServiceAccountOut(
        id=sa.id,
        name=sa.name,
        description=sa.description,
        status=sa.status,
        row_version=sa.row_version,
        created_at=sa.created_at,
        updated_at=sa.updated_at,
    )


def list_service_accounts(
    scope: TenantScope, *, limit: int, cursor: str | None
) -> Page[s.ServiceAccountOut]:
    scope.pdp.require("serviceaccount:read", scope.tenant_ref)
    rows = identity.list_service_accounts(
        scope.session, tenant_id=scope.tenant_id, after=decode_cursor(cursor), limit=limit + 1
    )
    page, more = paginate(rows, limit)
    return Page[s.ServiceAccountOut](
        items=[_sa_out(r) for r in page],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def _get_sa(scope: TenantScope, sa_id: UUID) -> ServiceAccount:
    sa = identity.get_service_account(scope.session, scope.tenant_id, sa_id)
    if sa is None:
        raise NotFound("service account not found")
    return sa


def get_service_account(scope: TenantScope, sa_id: UUID) -> s.ServiceAccountOut:
    scope.pdp.require("serviceaccount:read", scope.tenant_ref)
    return _sa_out(_get_sa(scope, sa_id))


def create_service_account(scope: TenantScope, body: s.ServiceAccountCreate) -> s.ServiceAccountOut:
    scope.pdp.require("serviceaccount:manage", scope.tenant_ref)
    iam.assert_active(scope)
    sa_id = identity.create_service_account(
        scope.session,
        tenant_id=scope.tenant_id,
        name=body.name,
        description=body.description,
        owner_id=scope.principal.principal_id,
    )
    iam.record(
        scope,
        "identity.service_account.created",
        target_type="service_account",
        target_id=sa_id,
        label=f"service_account:{body.name}",
        details={"name": body.name},
    )
    iam.commit(scope.session, "service account")
    return _sa_out(_get_sa(scope, sa_id))


def update_service_account(
    scope: TenantScope, sa_id: UUID, body: s.ServiceAccountPatch
) -> s.ServiceAccountOut:
    scope.pdp.require("serviceaccount:manage", scope.tenant_ref)
    iam.assert_active(scope)
    sa = _get_sa(scope, sa_id)
    sent = body.model_fields_set - {"row_version"}
    if not sent:
        raise ValidationFailed("no updatable fields supplied")
    values: dict[str, object] = {}
    for field in ("name", "description", "status"):
        if field in sent:
            value = getattr(body, field)
            if value is None and field != "description":
                continue
            values[field] = value
    if values.get("status") == "active" and sa.status != "active":
        bindings = authz.bindings_of_subject(
            scope.session, scope.tenant_id, "service_account", [sa.id]
        )
        iam.guard_bindings(scope, bindings, action="re-enable")
    # always conditioned on the row read above, so a concurrent disable between the read and the
    # write (which the re-enable guard did not see) is a conflict (admin review 3 item 1, B-1647)
    if body.row_version is not None and body.row_version != sa.row_version:
        raise Conflict("row_version mismatch: the service account was modified; reload and retry")
    try:
        with scope.session.begin_nested():
            updated = identity.update_service_account(
                scope.session, scope.tenant_id, sa.id, sa.row_version, values
            )
    except IntegrityError as exc:  # the name is taken in this client (its own list shows it)
        raise Conflict(SA_NAME_TAKEN) from exc
    if updated is None:
        raise Conflict("row_version mismatch: the service account was modified; reload and retry")
    keys_revoked = 0
    if values.get("status") == "disabled":  # disabling revokes every key immediately
        keys_revoked = identity.revoke_keys_of_principal(
            scope.session,
            scope.tenant_id,
            "service_account",
            sa.id,
            scope.principal.principal_id,
            utcnow(),
        )
    iam.record(
        scope,
        "identity.service_account.updated",
        target_type="service_account",
        target_id=sa.id,
        label=f"service_account:{sa.name}",
        details={"changed": sorted(values), "keys_revoked": keys_revoked},
    )
    iam.commit(scope.session, "service account")
    return _sa_out(updated)


# ------------------------------------------------------------------ API keys
def _key_status(key: ApiKey, now: datetime) -> str:
    if key.revoked_at is not None:
        return "revoked"
    return "expired" if key.expires_at <= now else "active"


def key_out(key: ApiKey) -> s.ApiKeyOut:
    return s.ApiKeyOut(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        principal_type=key.principal_type,
        principal_id=key.principal_id,
        scopes=list(key.scopes),
        resource_scope=key.resource_scope,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        rotated_from_id=key.rotated_from_id,
        created_at=key.created_at,
        status=_key_status(key, utcnow()),
    )


def list_api_keys(
    scope: TenantScope,
    *,
    principal_type: str | None,
    principal_id: UUID | None,
    limit: int,
    cursor: str | None,
) -> Page[s.ApiKeyOut]:
    scope.pdp.require("apikey:read", scope.tenant_ref)
    rows = identity.list_api_keys(
        scope.session,
        tenant_id=scope.tenant_id,
        principal_type=principal_type,
        principal_id=principal_id,
        after=decode_cursor(cursor),
        limit=limit + 1,
    )
    page, more = paginate(rows, limit)
    return Page[s.ApiKeyOut](
        items=[key_out(k) for k in page],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def _resolve_resource_scope(
    scope: TenantScope, raw: s.ResourceScopeIn | None, scopes: list[str]
) -> ResourceScope | None:
    """Validate the key's workspace/project ids (all inside this tenant) and that the creator
    holds every scope at each of them. Returns the mask, or None for an unrestricted key."""
    if raw is None or (not raw.workspace_ids and not raw.project_ids):
        iam.guard_grant(scope, scopes, scope.tenant_ref)
        return None
    for ws_id in dict.fromkeys(raw.workspace_ids):
        try:
            ref = tenancy.resolve_scope_ref(scope, "workspace", ws_id)
        except NotFound as exc:
            raise ValidationFailed("a workspace in resource_scope was not found") from exc
        iam.guard_grant(scope, scopes, ref)
    for project_id in dict.fromkeys(raw.project_ids):
        try:
            ref = tenancy.resolve_scope_ref(scope, "project", project_id)
        except NotFound as exc:
            raise ValidationFailed("a project in resource_scope was not found") from exc
        iam.guard_grant(scope, scopes, ref)
    return ResourceScope(frozenset(raw.workspace_ids), frozenset(raw.project_ids))


def _validate_scopes(scope: TenantScope, scopes: list[str]) -> list[str]:
    unique = list(dict.fromkeys(scopes))
    platform_only = sorted(set(unique) & authz.PLATFORM_ONLY_ACTIONS)
    if platform_only:
        raise ValidationFailed(f"{', '.join(platform_only)} cannot be an API key scope")
    unknown = sorted(set(unique) - authz.existing_permission_keys(scope.session, unique))
    if unknown:
        raise ValidationFailed(f"unknown scopes: {', '.join(unknown)}")
    return unique


def create_api_key(
    scope: TenantScope, settings: Settings, body: s.ApiKeyCreate
) -> tuple[s.ApiKeyCreated, UUID]:
    scope.pdp.require("apikey:manage", scope.tenant_ref)
    iam.assert_active(scope)
    if body.expires_in_days > settings.api_key_max_days:
        raise ValidationFailed(f"expires_in_days must be between 1 and {settings.api_key_max_days}")
    scopes = _validate_scopes(scope, body.scopes)
    mask = _resolve_resource_scope(scope, body.resource_scope, scopes)
    if body.principal_type == "service_account":
        sa = identity.get_service_account(scope.session, scope.tenant_id, body.principal_id)
        if sa is None or sa.status != "active":
            raise ValidationFailed("service account not found or not active")
    else:
        if (
            scope.principal.principal_type != "user"
            or body.principal_id != scope.principal.principal_id
        ):
            raise Forbidden("personal API keys can only be created by the user for themselves")
    key, secret = apikeys.mint(
        scope.session,
        settings,
        tenant_id=scope.tenant_id,
        name=body.name,
        principal_type=body.principal_type,
        principal_id=body.principal_id,
        scopes=scopes,
        resource_scope=mask,
        expires_at=utcnow() + timedelta(days=body.expires_in_days),
        created_by=scope.principal.principal_id,  # the caller's own id, never the body's (B-921)
    )
    iam.record(
        scope,
        "identity.api_key.created",
        target_type="api_key",
        target_id=key.id,
        label=f"api_key:{key.prefix}",
        details={
            "name": body.name,
            "prefix": key.prefix,
            "principal_type": body.principal_type,
            "principal_id": str(body.principal_id),
            "scopes": scopes,
            "resource_scope": key.resource_scope,
            "expires_at": key.expires_at.isoformat(),
        },
    )
    iam.commit(scope.session, "API key")
    return s.ApiKeyCreated(api_key=key_out(key), secret=secret), key.id


def _get_key(scope: TenantScope, key_id: UUID) -> ApiKey:
    key = identity.get_api_key(scope.session, scope.tenant_id, key_id)
    if key is None:
        raise NotFound("API key not found")
    return key


def rotate_api_key(
    scope: TenantScope, settings: Settings, key_id: UUID, body: s.ApiKeyRotate
) -> s.ApiKeyCreated:
    scope.pdp.require("apikey:manage", scope.tenant_ref)
    iam.assert_active(scope)
    old = _get_key(scope, key_id)
    if old.principal_type == "user" and (
        scope.principal.principal_type != "user" or old.principal_id != scope.principal.principal_id
    ):
        # A personal key is the user's own credential: others may revoke it, never re-mint it.
        raise Forbidden("personal API keys can only be rotated by their owner")
    now = utcnow()
    if old.revoked_at is not None or old.expires_at <= now:
        raise Conflict("only an active key can be rotated")
    mask = _resolve_resource_scope(
        scope,
        None
        if old.resource_scope is None
        else s.ResourceScopeIn(
            workspace_ids=[UUID(x) for x in old.resource_scope.get("workspace_ids", [])],
            project_ids=[UUID(x) for x in old.resource_scope.get("project_ids", [])],
        ),
        list(old.scopes),
    )
    lifetime = min(old.expires_at - old.created_at, timedelta(days=settings.api_key_max_days))
    new, secret = apikeys.mint(
        scope.session,
        settings,
        tenant_id=scope.tenant_id,
        name=old.name,
        principal_type=old.principal_type,
        principal_id=old.principal_id,
        scopes=list(old.scopes),
        resource_scope=mask,
        expires_at=now + lifetime,
        created_by=scope.principal.principal_id,
        rotated_from_id=old.id,
    )
    # `old` was loaded in this client above (carry_forward row 87); keyed by it too (B-1646)
    identity.shorten_api_key_expiry(
        scope.session, old.id, now + timedelta(hours=body.grace_hours), tenant_id=scope.tenant_id
    )
    iam.record(
        scope,
        "identity.api_key.rotated",
        target_type="api_key",
        target_id=old.id,
        label=f"api_key:{old.prefix}",
        details={
            "old_key_id": str(old.id),
            "new_key_id": str(new.id),
            "new_prefix": new.prefix,
            "grace_hours": body.grace_hours,
        },
    )
    iam.commit(scope.session, "API key")
    return s.ApiKeyCreated(api_key=key_out(new), secret=secret)


def revoke_api_key(scope: TenantScope, key_id: UUID) -> s.ApiKeyOut:
    scope.pdp.require("apikey:manage", scope.tenant_ref)
    iam.assert_active(scope)
    key = _get_key(scope, key_id)  # loaded in this client first, and the mutator keyed by it
    changed = identity.revoke_api_key(
        scope.session, key.id, scope.principal.principal_id, utcnow(), tenant_id=scope.tenant_id
    )
    if changed:
        iam.record(
            scope,
            "identity.api_key.revoked",
            target_type="api_key",
            target_id=key.id,
            label=f"api_key:{key.prefix}",
            details={"prefix": key.prefix},
        )
    iam.commit(scope.session, "API key")
    refreshed = _get_key(scope, key_id)
    scope.session.refresh(refreshed)
    return key_out(refreshed)
