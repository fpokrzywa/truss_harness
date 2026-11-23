"""Secrets use cases (SECURITY_MODEL section 5, FR-SEC-*).

* Values are envelope-encrypted (AES-256-GCM per-secret DEK wrapped by the KEK ring) and are
  write-only: this module never returns plaintext except through ``SecretProvider.resolve``.
* ``SecretProvider.resolve(ref, principal, purpose)`` is PDP-checked (``secret:use``) and audited,
  and writes ``secret_access_log``. ``resolve_system`` is the server-internal path (IdP client
  secret at token exchange) with no principal; it is audited as actor ``system``.
* ``last_used_at`` is a throttled (60 s), non-blocking touch that skips a row another transaction
  has locked (``FOR UPDATE SKIP LOCKED``), made after the audit event: a read no longer holds
  the secret row lock across ``audit.record``, whose chain lock a concurrent read of the same
  secret may hold, so concurrent reads cannot deadlock. ``last_used_at`` may lag by the interval,
  or longer while other reads of the secret are in flight (B-3952).
* Resolved plaintext is wrapped in ``SecretValue`` (repr/str are masked) and registered with the
  redaction registry so it can never reach logs, problem details or audit payloads verbatim.
* Reads return ``SecretMeta`` (metadata only): the ORM row with the ciphertext, wrapped DEK, nonce
  and key id stays inside this module (DECISIONS B-803).
* Every lookup is keyed by the owning tenant as well as by row-level security (B-802); the
  ``env`` provider serves installation-level secrets only (B-804).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.audit import service as audit
from shifde.authz.service import PDP, ResourceRef
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.crypto import CryptoError, Envelope, KeyRing
from shifde.core.db import get_context
from shifde.core.dbrole import sees_all_rows
from shifde.core.errors import Conflict, NotFound, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.redaction import register_secret
from shifde.secrets import repository
from shifde.secrets.models import Secret

__all__ = [
    "INSTALLATION_IDP_SECRET_RESOLVED",
    "MAX_SECRET_BYTES",
    "ROW_SECURITY_BLOCKS_REWRAP",
    "ROW_SECURITY_HIDES_SECRETS",
    "SECRET_NAME_CONFLICT",
    "RowSecurityHidesSecrets",
    "SecretMeta",
    "SecretProvider",
    "SecretValue",
    "check_all",
    "check_value",
    "create_secret",
    "disable_secret",
    "get_secret",
    "list_secrets",
    "rewrap_all",
    "rewrap_secret",
    "rotate_secret",
]

MAX_SECRET_BYTES = 16 * 1024
LAST_USED_INTERVAL_SECONDS = 60
SECRET_NAME_CONFLICT = "a secret with this name already exists"
INSTALLATION_IDP_SECRET_RESOLVED = "secret.idp_client_secret.resolved"
ROW_SECURITY_HIDES_SECRETS = (
    "the database role cannot see every client's secrets (needs BYPASSRLS); nothing was checked"
)
ROW_SECURITY_BLOCKS_REWRAP = (
    "the database role cannot see every client's secrets (needs BYPASSRLS); nothing was re-wrapped"
)
KINDS = frozenset({"generic", "api_key", "password", "token", "pem", "idp_client_secret"})


class SecretValue:
    """A resolved plaintext. repr/str never show it; call ``reveal()`` at the point of use."""

    __slots__ = ("_value",)

    def __init__(self, value: bytes) -> None:
        self._value = value
        register_secret(value)

    def reveal(self) -> str:
        return self._value.decode("utf-8")

    def __repr__(self) -> str:
        return "SecretValue(***)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class SecretMeta:
    """What callers outside this module see of a secret: never the value or its key material."""

    id: UUID
    tenant_id: UUID | None
    name: str
    kind: str
    provider_key: str
    scope_type: str
    scope_id: UUID | None
    secret_version: int
    status: str
    expires_at: datetime | None
    rotated_at: datetime | None
    last_used_at: datetime | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime | None
    row_version: int

    @classmethod
    def of(cls, row: Secret) -> SecretMeta:
        return cls(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            kind=row.kind,
            provider_key=row.provider_key,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            secret_version=row.secret_version,
            status=row.status,
            expires_at=row.expires_at,
            rotated_at=row.rotated_at,
            last_used_at=row.last_used_at,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            row_version=row.row_version,
        )


def _check_value(value: str) -> bytes:
    raw = value.encode("utf-8")
    if not raw or len(raw) > MAX_SECRET_BYTES:
        raise ValidationFailed(f"secret value must be 1..{MAX_SECRET_BYTES} bytes")
    register_secret(value)
    return raw


def check_value(value: str) -> None:
    """Refuse a value `create_secret`/`rotate_secret` would refuse (422, never echoing it), for a
    caller whose earlier checks must answer before its later ones (connections, B-2749)."""
    _check_value(value)


def get_secret(session: Session, secret_id: UUID, *, tenant_id: UUID | None) -> SecretMeta | None:
    """The secret `secret_id` if `tenant_id` owns it (`None` = installation level)."""
    row = repository.get_secret(session, secret_id, tenant_id=tenant_id)
    return None if row is None else SecretMeta.of(row)


def list_secrets(
    session: Session, *, tenant_id: UUID | None, after: UUID | None, limit: int
) -> list[SecretMeta]:
    rows = repository.list_secrets(session, tenant_id=tenant_id, after=after, limit=limit)
    return [SecretMeta.of(row) for row in rows]


def create_secret(
    session: Session,
    keyring: KeyRing,
    *,
    tenant_id: UUID | None,
    name: str,
    value: str,
    kind: str = "generic",
    scope_type: str = "tenant",
    scope_id: UUID | None = None,
    created_by: UUID | None,
) -> SecretMeta:
    if kind not in KINDS:
        raise ValidationFailed("unknown secret kind")
    raw = _check_value(value)
    secret_id = uuid7()
    env = keyring.encrypt(raw, tenant_id=tenant_id, secret_id=secret_id, version=1)
    try:
        repository.insert_secret(
            session,
            {
                "id": secret_id,
                "tenant_id": tenant_id,
                "name": name,
                "kind": kind,
                "provider_key": "db_encrypted",
                "scope_type": scope_type,
                "scope_id": scope_id,
                "ciphertext": env.ciphertext,
                "wrapped_dek": env.wrapped_dek,
                "nonce": env.nonce,
                "kek_id": env.kek_id,
                "secret_version": 1,
                "status": "active",
                "created_by": created_by,
                "created_at": utcnow(),
            },
        )
        session.flush()
    except IntegrityError as exc:
        # every integrity refusal (the name index, the scope trigger `trg_secrets_scope`, a
        # CHECK) is the one name-conflict answer: none says whether another client's workspace,
        # project or name exists (tenancy review 1 item 6, B-1644)
        session.rollback()
        raise Conflict(SECRET_NAME_CONFLICT) from exc
    created = repository.get_secret(session, secret_id, tenant_id=tenant_id)
    if created is None:  # pragma: no cover
        raise NotFound("secret not found")
    return SecretMeta.of(created)


def rotate_secret(
    session: Session,
    keyring: KeyRing,
    secret: SecretMeta,
    *,
    value: str,
    expected_version: int | None,
) -> SecretMeta:
    if secret.status != "active":
        raise Conflict("a disabled secret cannot be rotated")
    raw = _check_value(value)
    new_version = secret.secret_version + 1
    env = keyring.encrypt(raw, tenant_id=secret.tenant_id, secret_id=secret.id, version=new_version)
    updated = repository.update_secret(
        session,
        secret.id,
        expected_version,
        {
            "ciphertext": env.ciphertext,
            "wrapped_dek": env.wrapped_dek,
            "nonce": env.nonce,
            "kek_id": env.kek_id,
            "secret_version": new_version,
            "rotated_at": utcnow(),
        },
        tenant_id=secret.tenant_id,
    )
    if updated is None:
        raise Conflict("row_version mismatch: the secret was modified; reload and retry")
    return SecretMeta.of(updated)


def disable_secret(
    session: Session, secret: SecretMeta, *, expected_version: int | None
) -> SecretMeta:
    updated = repository.update_secret(
        session, secret.id, expected_version, {"status": "disabled"}, tenant_id=secret.tenant_id
    )
    if updated is None:
        raise Conflict("row_version mismatch: the secret was modified; reload and retry")
    return SecretMeta.of(updated)


def rewrap_secret(session: Session, keyring: KeyRing, secret: Secret) -> bool:
    """Re-wrap the DEK under the active KEK (owner/CLI path). True if the row changed."""
    if secret.provider_key != "db_encrypted" or secret.wrapped_dek is None or not secret.kek_id:
        return False
    if secret.kek_id == keyring.active_id:
        return False
    wrapped, kid = keyring.rewrap(
        secret.wrapped_dek, tenant_id=secret.tenant_id, secret_id=secret.id, old_kid=secret.kek_id
    )
    updated = repository.update_secret(
        session,
        secret.id,
        None,
        {"wrapped_dek": wrapped, "kek_id": kid},
        tenant_id=secret.tenant_id,
        bump=False,
    )
    return updated is not None


def rewrap_all(session: Session, keyring: KeyRing) -> tuple[int, int]:
    """KEK rotation: re-wrap every DEK under the active KEK. Returns (rewrapped, total). Raises
    ``RowSecurityHidesSecrets`` (``ROW_SECURITY_BLOCKS_REWRAP``) before reading any row unless the
    role is a superuser or has BYPASSRLS (otherwise row-level security would hide rows and
    ``(0, 0)`` look like success)."""
    if not sees_all_rows(session):
        raise RowSecurityHidesSecrets(ROW_SECURITY_BLOCKS_REWRAP)
    rows = repository.all_secrets(session)
    changed = sum(1 for sec in rows if rewrap_secret(session, keyring, sec))
    return changed, len(rows)


class RowSecurityHidesSecrets(Exception):
    """The session's role is subject to row-level security, so not every secret is visible."""

    def __init__(self, message: str = ROW_SECURITY_HIDES_SECRETS) -> None:
        super().__init__(message)


def check_all(session: Session, keyring: KeyRing) -> tuple[int, list[UUID]]:
    """Decrypt every ``db_encrypted`` envelope (any status) and discard the plaintext at once:
    nothing is logged, audited or written. Returns (ok, ids that failed). Raises
    ``RowSecurityHidesSecrets`` before reading any row unless the role is a superuser or has
    BYPASSRLS (otherwise row-level security would hide rows and ``ok 0, failed 0`` mean nothing)."""
    if not sees_all_rows(session):
        raise RowSecurityHidesSecrets
    ok = 0
    failed: list[UUID] = []
    for sec in repository.all_secrets(session):
        if sec.provider_key != "db_encrypted":
            continue
        if not (sec.ciphertext and sec.wrapped_dek and sec.nonce and sec.kek_id):
            failed.append(sec.id)
            continue
        try:
            keyring.decrypt(
                Envelope(sec.ciphertext, sec.wrapped_dek, sec.nonce, sec.kek_id),
                tenant_id=sec.tenant_id,
                secret_id=sec.id,
                version=sec.secret_version,
            )
        except CryptoError:
            failed.append(sec.id)
            continue
        ok += 1
    return ok, failed


class SecretProvider:
    """``db_encrypted`` provider (the ``env`` provider reads a named environment variable)."""

    def __init__(self, session: Session, keyring: KeyRing, ctx: RequestContext) -> None:
        self._session = session
        self._keyring = keyring
        self._ctx = ctx

    def _scope_ref(self, secret: Secret, tenant_id: UUID) -> ResourceRef:
        if secret.scope_type == "workspace" and secret.scope_id:
            return ResourceRef(tenant_id, workspace_id=secret.scope_id)
        if secret.scope_type == "project" and secret.scope_id:
            ws = repository.project_workspace_id(
                self._session, secret.scope_id, tenant_id=tenant_id
            )
            return ResourceRef(tenant_id, workspace_id=ws, project_id=secret.scope_id)
        return ResourceRef(tenant_id)

    def resolve(
        self, ref: UUID, principal: Principal, purpose: str, *, pdp: PDP, run_id: UUID | None = None
    ) -> SecretValue:
        """PDP-checked (``secret:use`` at the secret scope), audited resolution of a secret of
        the session's tenant (installation-level secrets are never resolved for a principal).
        A session whose client is not the PDP's (a mis-set context) is refused before the lookup
        with the answer an unknown id gets (secrets review 1 item 2, B-1643)."""
        tenant_id = get_context(self._session).tenant_id
        if tenant_id is None or tenant_id != pdp.tenant_id:
            raise NotFound("secret not found")
        secret = repository.get_secret(self._session, ref, tenant_id=tenant_id)
        if secret is None or secret.tenant_id is None or secret.status != "active":
            raise NotFound("secret not found")
        pdp.require(
            "secret:use", self._scope_ref(secret, secret.tenant_id), hide_unless="secret:read"
        )
        value = self._decrypt(secret)
        used_at = self._log(secret, principal.principal_id, purpose, run_id)
        audit.record(
            self._session,
            self._ctx,
            action="secret.resolved",
            tenant_id=secret.tenant_id,
            target_type="secret",
            target_id=secret.id,
            target_label=f"secret:{secret.name}",
            details={"purpose": purpose, "version": secret.secret_version},
        )
        self._touch(secret, used_at)
        return value

    def resolve_system(self, ref: UUID, purpose: str, *, tenant_id: UUID | None) -> SecretValue:
        """Server-internal resolution with no principal (e.g. IdP client secret at the token
        exchange). The caller establishes the tenant / platform-admin DB context and names the
        tenant that owns the reference (`None` = installation level); a secret of any other
        tenant is not found. No PDP check: a caller acting for a principal checks ``secret:use``
        itself (finding C2, connections)."""
        secret = repository.get_secret(self._session, ref, tenant_id=tenant_id)
        if secret is None or secret.status != "active":
            raise NotFound("secret not found")
        value = self._decrypt(secret)
        used_at = self._log(secret, None, purpose, None)
        audit.record(
            self._session,
            self._ctx,
            action="secret.resolved",
            tenant_id=secret.tenant_id,
            actor_label="system",
            target_type="secret",
            target_id=secret.id,
            target_label=f"secret:{secret.name}",
            details={"purpose": purpose, "version": secret.secret_version},
        )
        self._touch(secret, used_at)
        return value

    def resolve_installation_idp(self, idp_id: UUID) -> SecretValue:
        """An installation IdP's client secret for the OIDC token exchange (identity review 4
        item 5, B-2260), with no principal, no client context and no platform-administrator
        context: `shifde_installation_idp_secret` returns the envelope only for an active
        installation IdP's active installation secret and writes the access-log row; a client
        context is refused by the database (42501). Audited at installation level as
        `secret.idp_client_secret.resolved` (actor `system`)."""
        secret = repository.installation_idp_secret(self._session, idp_id)
        if secret is None:
            raise NotFound("secret not found")
        value = self._decrypt(secret)
        audit.record(
            self._session,
            self._ctx,
            action=INSTALLATION_IDP_SECRET_RESOLVED,
            tenant_id=None,
            actor_label="system",
            target_type="secret",
            target_id=secret.id,
            target_label=f"secret:{secret.name}",
            details={
                "purpose": "oidc.token_exchange",
                "version": secret.secret_version,
                "idp_id": str(idp_id),
            },
        )
        return value

    def _decrypt(self, secret: Secret) -> SecretValue:
        if secret.provider_key == "env":
            if secret.tenant_id is not None:  # a client never reads the server's environment
                raise NotFound("secret not found")
            raw = os.environ.get(secret.external_ref or "")
            if raw is None:
                raise NotFound("secret not found")
            return SecretValue(raw.encode())
        assert secret.ciphertext and secret.wrapped_dek and secret.nonce and secret.kek_id
        try:
            plain = self._keyring.decrypt(
                Envelope(secret.ciphertext, secret.wrapped_dek, secret.nonce, secret.kek_id),
                tenant_id=secret.tenant_id,
                secret_id=secret.id,
                version=secret.secret_version,
            )
        except CryptoError as exc:
            raise NotFound("secret could not be decrypted") from exc
        return SecretValue(plain)

    def _log(
        self, secret: Secret, principal_id: UUID | None, purpose: str, run_id: UUID | None
    ) -> datetime:
        now: datetime = utcnow()
        repository.insert_access(
            self._session,
            {
                "id": uuid7(),
                "tenant_id": secret.tenant_id,
                "secret_id": secret.id,
                "at": now,
                "principal_id": principal_id,
                "purpose": purpose[:200],
                "run_id": run_id,
                "outcome": "success",
            },
        )
        return now

    def _touch(self, secret: Secret, now: datetime) -> None:
        repository.touch_secret(
            self._session, secret.id, now, LAST_USED_INTERVAL_SECONDS, tenant_id=secret.tenant_id
        )
