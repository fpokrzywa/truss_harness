"""Identity-provider configuration (admin part 3a), secrets metadata/rotation and the audit read
API (part 3b).

IdP client secrets are write-only: they are stored through the secrets module (envelope
encrypted) and referenced by id; responses only say `client_secret_set`.

Ported from SETH 3ba0e06 `admin/providers.py`. Changes (DECISIONS B-1621.., B-1641..):

* The installation scope is the platform administrator's only, through
  `tenancy.service.require_platform_admin` (B-1011: one 403 for everyone else whatever the id);
  its rows are read and written under `platform_admin_context` after that check and filtered
  `tenant_id IS NULL`, so no installation route lists or reaches a client's IdP (identity
  review 4 item 7). A client's scope is `resolve_org` (B-1019) and its rows are read with the
  client's id as well as row-level security.
* A client secret is created in the IdP's own scope (installation or that client), so an
  installation IdP holds only an installation secret and a client's IdP only that client's
  (`trg_idp_configs_secret_scope`, `fk_idp_configs__client_secret`, B-924). Every integrity
  refusal of an IdP write (that trigger, that key, `uq_idp_configs__scope_issuer`) is the one
  `IssuerConflict` answer `IDP_CONFLICT` (identity review 1 item 3): the issuer index is per
  scope, so the answer says nothing about another client.
* `identity.update_idp` (id-only filter, carry_forward 85) runs only on a row first loaded in
  the caller's scope; a rotated secret is read with the scope's tenant id.
* A client's IdP never syncs groups (`ck_idp_configs__group_sync_installation_only`, B-932) and
  never provisions people (`ck_idp_configs__tenant_no_jit`): both are a 422 here, before the
  database refuses.
* Secrets (B-1641): every lookup names the scope's client (`get_secret(..., tenant_id=)`,
  B-802) and responses are built from `SecretMeta` (B-803), so another client's secret id is
  exactly an unknown id and a client never reaches an installation secret. Values never reach a
  response or an audit payload.
* Audit (B-1642): events and the chain check are the scope's client's only
  (`audit.list_events(tenant_id=)`, `audit.verify_chain(tenant_id)`, under row-level security
  as well).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.admin import iam
from shifde.admin import schemas as s
from shifde.audit import service as audit
from shifde.authz.service import PDP, ResourceRef
from shifde.core import db
from shifde.core.context import Principal, RequestContext
from shifde.core.errors import (
    Conflict,
    Forbidden,
    IssuerConflict,
    NotFound,
    ScopeArchived,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.core.settings import Settings
from shifde.core.ssrf import EgressViolation, check_scheme
from shifde.identity import service as identity
from shifde.identity.oidc import OidcClient
from shifde.identity.service import IdpConfig
from shifde.secrets import service as secrets_service
from shifde.secrets.service import SecretMeta
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

IDP_CONFLICT = "the identity provider conflicts with another identity provider in this scope"
IDP_NOT_FOUND = "identity provider not found"
SECRET_NOT_FOUND = "secret not found"


@dataclass
class IdpScope:
    """Where an IdP operation runs: one client (`tenant_id` set) or the installation (None)."""

    session: Session
    ctx: RequestContext
    principal: Principal
    tenant_id: UUID | None
    pdp: PDP | None
    tenant_status: str = "active"

    @classmethod
    def for_tenant(cls, scope: TenantScope) -> IdpScope:
        return cls(
            scope.session,
            scope.ctx,
            scope.principal,
            scope.tenant_id,
            scope.pdp,
            scope.tenant_status,
        )

    @classmethod
    def for_platform(cls, session: Session, ctx: RequestContext, principal: Principal) -> IdpScope:
        return cls(session, ctx, principal, None, None)

    def require_read(self) -> None:
        if self.tenant_id is None:
            tenancy.require_platform_admin(self.session, self.principal)
            return
        assert self.pdp is not None
        self.pdp.require("idp:manage", ResourceRef(self.tenant_id))

    def require(self) -> None:
        self.require_read()
        if self.tenant_status != "active":
            raise ScopeArchived("organization is archived")

    @contextmanager
    def db_context(self) -> Iterator[None]:
        """Installation rows need the platform-administrator context (after the check above);
        a client's rows are read in the client context `resolve_org` set."""
        if self.tenant_id is None:
            with db.platform_admin_context(self.session):
                yield
        else:
            yield

    def audit(self, action: str, idp_id: UUID, label: str, details: dict[str, object]) -> None:
        audit.record(
            self.session,
            self.ctx,
            action=action,
            tenant_id=self.tenant_id,
            target_type="idp",
            target_id=idp_id,
            target_label=f"idp:{label}",
            details=details,
        )


def _idp_out(idp: IdpConfig) -> s.IdpOut:
    return s.IdpOut(
        id=idp.id,
        tenant_id=idp.tenant_id,
        display_name=idp.display_name,
        issuer=idp.issuer,
        discovery_url=idp.discovery_url,
        client_id=idp.client_id,
        client_secret_set=idp.client_secret_ref_id is not None,
        scopes=list(idp.scopes),
        claim_mappings=dict(idp.claim_mappings),
        jit=idp.jit,
        group_sync=idp.group_sync_mode,
        status=idp.status,
        row_version=idp.row_version,
        created_at=idp.created_at,
        updated_at=idp.updated_at,
    )


def _check_client_idp(sc: IdpScope, jit: str | None, group_sync: str | None) -> None:
    """A client's IdP only signs in existing members: it never provisions people and never
    syncs platform groups (B-932)."""
    if sc.tenant_id is None:
        return
    if jit not in (None, "off"):
        raise ValidationFailed("jit must be 'off' for tenant identity providers")
    if group_sync not in (None, "off"):
        raise ValidationFailed("group_sync must be 'off' for tenant identity providers")


def _check_urls(settings: Settings, *urls: str) -> None:
    for url in urls:
        try:
            check_scheme(url, allow_dev_loopback=settings.is_development)
        except EgressViolation as exc:
            raise ValidationFailed(f"invalid URL: {exc}") from exc


def _get(sc: IdpScope, idp_id: UUID) -> IdpConfig:
    idp = identity.get_idp(sc.session, sc.tenant_id, idp_id)
    if idp is None:
        raise NotFound(IDP_NOT_FOUND)
    return idp


def list_idps(sc: IdpScope, *, limit: int, cursor: str | None) -> Page[s.IdpOut]:
    sc.require_read()
    with sc.db_context():
        rows = identity.list_idps(
            sc.session, tenant_id=sc.tenant_id, after=decode_cursor(cursor), limit=limit + 1
        )
        page, more = paginate(rows, limit)
        items = [_idp_out(r) for r in page]
        # read inside the context too (B-1020a): an installation row is not readable after it
        next_cursor = encode_cursor(page[-1].id) if more and page else None
    return Page[s.IdpOut](items=items, next_cursor=next_cursor)


def get_idp(sc: IdpScope, idp_id: UUID) -> s.IdpOut:
    sc.require_read()
    with sc.db_context():
        return _idp_out(_get(sc, idp_id))


def _new_secret(sc: IdpScope, settings: Settings, idp_id: UUID, value: str) -> UUID:
    """The IdP's client secret, owned by the IdP's own scope (B-924)."""
    secret = secrets_service.create_secret(
        sc.session,
        settings.keyring,
        tenant_id=sc.tenant_id,
        name=f"idp:{idp_id}:client_secret",
        value=value,
        kind="idp_client_secret",
        created_by=sc.principal.principal_id,
    )
    return secret.id


def create_idp(sc: IdpScope, settings: Settings, body: s.IdpCreate) -> s.IdpOut:
    sc.require()
    _check_urls(settings, body.issuer, body.discovery_url)
    _check_client_idp(sc, body.jit, body.group_sync)
    idp_id = uuid7()
    try:
        with sc.db_context():
            secret_ref = (
                _new_secret(sc, settings, idp_id, body.client_secret)
                if body.client_secret
                else None
            )
            try:
                with sc.session.begin_nested():
                    identity.create_idp(
                        sc.session,
                        {
                            "id": idp_id,
                            "tenant_id": sc.tenant_id,
                            "display_name": body.display_name,
                            "issuer": body.issuer,
                            "discovery_url": body.discovery_url,
                            "client_id": body.client_id,
                            "client_secret_ref_id": secret_ref,
                            "scopes": body.scopes,
                            "claim_mappings": body.claim_mappings,
                            "jit": body.jit,
                            "group_sync_mode": body.group_sync,
                            "status": body.status,
                            "created_by": sc.principal.principal_id,
                        },
                    )
            except IntegrityError as exc:
                raise IssuerConflict(IDP_CONFLICT) from exc
            sc.audit(
                "identity.idp.created",
                idp_id,
                body.display_name,
                {
                    "issuer": body.issuer,
                    "client_id": body.client_id,
                    "jit": body.jit,
                    "group_sync": body.group_sync,
                    "client_secret_set": secret_ref is not None,
                },
            )
            sc.session.flush()
            out = _idp_out(_get(sc, idp_id))
    except BaseException:
        sc.session.rollback()
        raise
    iam.commit(sc.session, "identity provider")
    return out


def _require_secret_use_for_destination(
    sc: IdpScope, idp: IdpConfig, body: s.IdpPatch, sent: set[str]
) -> None:
    """The client secret is sent to the token endpoint named by the discovery document of
    `issuer` / `discovery_url`. Moving either while the same secret stays bound needs `secret:use`
    on it (as for connections, FR-CON-011); replacing the secret in the same request does not
    (the old value is gone). Installation-level IdPs are platform-administrator only."""
    if sc.tenant_id is None or idp.client_secret_ref_id is None or body.client_secret:
        return
    moved = ("issuer" in sent and body.issuer not in (None, idp.issuer)) or (
        "discovery_url" in sent and body.discovery_url not in (None, idp.discovery_url)
    )
    if not moved:
        return
    assert sc.pdp is not None
    try:
        sc.pdp.require("secret:use", ResourceRef(sc.tenant_id))
    except Forbidden as exc:
        raise Forbidden(
            "changing the issuer or discovery URL of an identity provider whose client secret "
            "stays bound needs secret:use on that secret (or supply a new client_secret in the "
            "same request)"
        ) from exc


_PATCH_COLUMNS = (
    ("display_name", "display_name"),
    ("issuer", "issuer"),
    ("discovery_url", "discovery_url"),
    ("client_id", "client_id"),
    ("scopes", "scopes"),
    ("claim_mappings", "claim_mappings"),
    ("jit", "jit"),
    ("group_sync", "group_sync_mode"),
    ("status", "status"),
)


def _replace_secret(
    sc: IdpScope, settings: Settings, idp: IdpConfig, value: str, values: dict[str, object]
) -> None:
    """Rotate the bound secret, read in the IdP's own scope, or bind a new one there."""
    existing = (
        secrets_service.get_secret(sc.session, idp.client_secret_ref_id, tenant_id=sc.tenant_id)
        if idp.client_secret_ref_id
        else None
    )
    if existing is not None:
        secrets_service.rotate_secret(
            sc.session, settings.keyring, existing, value=value, expected_version=None
        )
    else:
        values["client_secret_ref_id"] = _new_secret(sc, settings, idp.id, value)


def update_idp(sc: IdpScope, settings: Settings, idp_id: UUID, body: s.IdpPatch) -> s.IdpOut:
    sc.require()
    sent = body.model_fields_set - {"row_version"}
    if not sent:
        raise ValidationFailed("no updatable fields supplied")
    _check_client_idp(sc, body.jit, body.group_sync)
    _check_urls(settings, *[u for u in (body.issuer, body.discovery_url) if u])
    try:
        with sc.db_context():
            idp = _get(sc, idp_id)  # carry_forward 85: loaded in the caller's scope first
            values: dict[str, object] = {}
            for field, column in _PATCH_COLUMNS:
                value = getattr(body, field)
                if field in sent and value is not None:
                    values[column] = value
            _require_secret_use_for_destination(sc, idp, body, sent)
            if body.client_secret:
                _replace_secret(sc, settings, idp, body.client_secret, values)
            try:
                with sc.session.begin_nested():
                    updated = identity.update_idp(sc.session, idp.id, body.row_version, values)
            except IntegrityError as exc:
                raise IssuerConflict(IDP_CONFLICT) from exc
            if updated is None:
                raise Conflict("row_version mismatch: the identity provider was modified; reload")
            sc.audit(
                "identity.idp.updated",
                idp.id,
                idp.display_name,
                {
                    "changed": sorted(sent - {"client_secret"}),
                    "client_secret_changed": bool(body.client_secret),
                },
            )
            out = _idp_out(updated)
    except BaseException:
        sc.session.rollback()
        raise
    iam.commit(sc.session, "identity provider")
    return out


def test_idp(sc: IdpScope, client: OidcClient, idp_id: UUID) -> s.IdpTestOut:
    sc.require()
    with sc.db_context():
        idp = _get(sc, idp_id)
        results = client.run_checks(idp.discovery_url, idp.issuer)
        ok = all(r[1] for r in results)
        sc.audit(
            "identity.idp.tested",
            idp.id,
            idp.display_name,
            {"ok": ok, "checks": [r[0] for r in results]},
        )
    iam.commit(sc.session, "identity provider")
    return s.IdpTestOut(ok=ok, checks=[s.IdpCheck(name=n, ok=o, detail=d) for n, o, d in results])


# ================================================================== secrets (part 3b)
def _secret_out(sec: SecretMeta) -> s.SecretOut:
    scope = (
        None
        if sec.scope_type == "tenant"
        else s.SecretScope.model_validate({"type": sec.scope_type, "id": sec.scope_id})
    )
    return s.SecretOut(
        id=sec.id,
        name=sec.name,
        kind=sec.kind,
        scope=scope,
        status=sec.status,
        version=sec.secret_version,
        created_at=sec.created_at,
        rotated_at=sec.rotated_at,
        last_used_at=sec.last_used_at,
        row_version=sec.row_version,
    )


def list_secrets(scope: TenantScope, *, limit: int, cursor: str | None) -> Page[s.SecretOut]:
    scope.pdp.require("secret:read", scope.tenant_ref)
    rows = secrets_service.list_secrets(
        scope.session, tenant_id=scope.tenant_id, after=decode_cursor(cursor), limit=limit + 1
    )
    page, more = paginate(rows, limit)
    return Page[s.SecretOut](
        items=[_secret_out(r) for r in page],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def _get_secret(scope: TenantScope, secret_id: UUID) -> SecretMeta:
    """The secret if this client owns it: another client's id, an installation secret and an
    unknown id are one answer (B-802)."""
    sec = secrets_service.get_secret(scope.session, secret_id, tenant_id=scope.tenant_id)
    if sec is None:
        raise NotFound(SECRET_NOT_FOUND)
    return sec


def _secret_ref(scope: TenantScope, scope_type: str, scope_id: UUID | None) -> ResourceRef:
    if scope_id is None:
        return scope.tenant_ref
    return tenancy.resolve_scope_ref(scope, scope_type, scope_id)


def create_secret(scope: TenantScope, settings: Settings, body: s.SecretCreate) -> s.SecretOut:
    scope_type = body.scope.type if body.scope else "tenant"
    scope_id = body.scope.id if body.scope else None
    if (scope_type == "tenant") != (scope_id is None):
        raise ValidationFailed("scope.id is required for workspace/project scopes only")
    try:
        ref = _secret_ref(scope, scope_type, scope_id)
    except NotFound as exc:  # another client's workspace or project is an unknown one
        raise ValidationFailed("scope not found in this organization") from exc
    scope.pdp.require("secret:manage", ref)
    iam.assert_active(scope)
    sec = secrets_service.create_secret(
        scope.session,
        settings.keyring,
        tenant_id=scope.tenant_id,
        name=body.name,
        value=body.value,
        kind=body.kind,
        scope_type=scope_type,
        scope_id=scope_id,
        created_by=scope.principal.principal_id,
    )
    iam.record(
        scope,
        "secret.created",
        target_type="secret",
        target_id=sec.id,
        label=f"secret:{body.name}",
        details={"name": body.name, "kind": body.kind, "scope_type": scope_type},
    )
    iam.commit(scope.session, "secret")
    return _secret_out(sec)


def rotate_secret(
    scope: TenantScope, settings: Settings, secret_id: UUID, body: s.SecretRotate
) -> s.SecretOut:
    scope.pdp.require("secret:manage", scope.tenant_ref)  # tenant-level, before any lookup
    sec = _get_secret(scope, secret_id)
    iam.assert_active(scope)
    updated = secrets_service.rotate_secret(
        scope.session, settings.keyring, sec, value=body.value, expected_version=body.row_version
    )
    iam.record(
        scope,
        "secret.rotated",
        target_type="secret",
        target_id=sec.id,
        label=f"secret:{sec.name}",
        details={"version": updated.secret_version},
    )
    iam.commit(scope.session, "secret")
    return _secret_out(updated)


def disable_secret(scope: TenantScope, secret_id: UUID, body: s.SecretPatch) -> s.SecretOut:
    scope.pdp.require("secret:manage", scope.tenant_ref)  # tenant-level, before any lookup
    sec = _get_secret(scope, secret_id)
    iam.assert_active(scope)
    updated = secrets_service.disable_secret(scope.session, sec, expected_version=body.row_version)
    iam.record(
        scope,
        "secret.disabled",
        target_type="secret",
        target_id=sec.id,
        label=f"secret:{sec.name}",
        details={},
    )
    iam.commit(scope.session, "secret")
    return _secret_out(updated)


# ================================================================== audit (part 3b)
def _event_out(e: audit.EventRow) -> s.AuditEventOut:
    return s.AuditEventOut(
        id=e.id,
        occurred_at=e.occurred_at,
        actor=e.actor,
        actor_id=e.actor_id,
        actor_type=e.actor_type,
        action=e.action,
        target=e.target,
        target_type=e.target_type,
        target_id=e.target_id,
        outcome=e.outcome,
        request_id=e.request_id,
        auth_method=e.auth_method,
        actor_ip=e.actor_ip,
        workspace_id=e.workspace_id,
        project_id=e.project_id,
        details=e.details,
        chain_seq=e.chain_seq,
        hash=e.hash_hex,
    )


def list_audit_events(
    scope: TenantScope,
    *,
    category: str | None,
    actor: str | None,
    action: str | None,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
    limit: int,
    cursor: str | None,
) -> Page[s.AuditEventOut]:
    sf = scope.pdp.visible_scopes("audit:read")
    limited = not sf.tenant_wide
    if limited and not sf.workspace_ids and not sf.project_ids:
        scope.pdp.require("audit:read", scope.tenant_ref)  # the standard 403
    rows = audit.list_events(
        scope.session,
        tenant_id=scope.tenant_id,
        category=category,
        actor=actor,
        action=action,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        before=decode_cursor(cursor),
        limit=limit + 1,
        # scoped visibility (FR-AUD-007): a workspace / project auditor sees only its scopes
        workspace_ids=sf.workspace_ids if limited else None,
        project_ids=sf.project_ids if limited else None,
    )
    page, more = paginate(rows, limit)
    return Page[s.AuditEventOut](
        items=[_event_out(e) for e in page],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def verify_audit(scope: TenantScope) -> s.AuditVerifyOut:
    scope.pdp.require("audit:read", scope.tenant_ref)
    result = audit.verify_chain(scope.session, scope.tenant_id)
    return s.AuditVerifyOut(
        ok=result.ok, checked=result.checked, first_broken_id=result.first_broken_id
    )
