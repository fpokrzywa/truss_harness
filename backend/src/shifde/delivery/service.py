"""Deployment targets and the one-platform-per-organization rule (design sections 3 and 9,
DECISIONS B-2501, lead ruling B-2524; SP6 design 6.4).

A target is a Databricks workspace or a Snowflake account (`platform_kind`). Its address is
normalized and allow-listed by that platform's normalizer in `adapters.native.addresses` before
anything is written (this module makes no outbound request). On one Snowflake claim, each package
and each application is bound by at most one active target (`409 binding_in_use`, checked under
the claim's advisory lock). The organization's claim on the address comes
from the `shifde.delivery_claim_platform` definer; when another organization holds the address the
caller gets the fixed `409 platform_already_registered`, which names no organization, row,
constraint or key. Archiving the last active target on a claim deletes the claim, so another
organization may register the address afterwards.

Executors (design 5, section 9 items 4, 12, 13 and 15; B-2664, B-2665): one active executor
identity per organization, at most two live `shx_` credentials each, stored as
`apikeys.hash_key(pepper, token)`. `authenticate_executor` resolves a token through the
`shifde.delivery_resolve_executor` definer before any client context exists.

Plan jobs (design 4, section 9 items 5 and 9; B-2675..B-2679): a leased `plan` job moves its plan
to `planning`; its result fails the plan or seals it (`plan_digest`, `sealed_at` on the database
clock, 15-minute expiry) in the result's transaction. `settle_plans` fails plans whose plan job was
lost and expires sealed plans past `expires_at`. The plan routes are `delivery.plans`.

Deployments (design 4, section 9 items 5, 6, 8, 10, 11 and 18; B-2681..B-2687): the `apply`,
`observe`, `verify` and `reconcile` result hooks drive a deployment from `leased` to `verified`,
`failed` or `unknown_outcome` in the result's transaction, and `settle_deployments` applies the
settle rules after the lost-job sweep. Every move to `unknown_outcome` (a failed apply after a
journal step, a succeeded apply with an open intent, settle rule (a) for a lost apply) writes one
`delivery.deployment.unknown_outcome` audit row in the same transaction, under the fixed system
context `UNKNOWN_OUTCOME_CONTEXT` (no actor; B-2736). The deployment routes are
`delivery.deployments`.

Every row is read and written for the caller's resolved client only (`TenantScope.tenant_id`).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal  # Any: target, executor and credential rows
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.native import planning
from shifde.adapters.native.addresses import AddressRefused, address_key, normalize_address
from shifde.adapters.native.contracts import (
    ApplyReceipt,
    JobResultSubmission,
    NativeObservation,
    PlanJobResult,
    ReconciliationResult,
    VerificationReport,
    digest,
)
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.errors import (
    AppError,
    Conflict,
    InvalidCredentials,
    NotFound,
    ScopeArchived,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.core.redaction import register_secret
from shifde.delivery import repository
from shifde.delivery import schemas as s
from shifde.identity import apikeys
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

EXECUTOR_TOKEN_REJECTED = "delivery.executor.token.rejected"
DEPLOYMENT_UNKNOWN_OUTCOME = "delivery.deployment.unknown_outcome"
# A system event: it may happen inside an unrelated request, so it never names that caller
UNKNOWN_OUTCOME_CONTEXT = RequestContext(request_id="delivery-unknown-outcome")
PLATFORM_ALREADY_REGISTERED = "This platform address is already registered as a deployment target."


class PlatformAlreadyRegistered(AppError):
    """Another organization holds the platform address (one fixed message, B-2501)."""

    status, code, title = 409, "platform_already_registered", "Conflict"

    def __init__(self) -> None:
        super().__init__(PLATFORM_ALREADY_REGISTERED)


class BindingInUse(AppError):
    """Another active target on the claim binds the same Snowflake package or application."""

    status, code, title = 409, "binding_in_use", "Conflict"

    def __init__(self) -> None:
        super().__init__(
            "Another active target on this account already binds this package or application."
        )


class PlanJobTargetMismatch(AppError):
    """A `plan` job's `target_id` is not its plan's target (B-2732)."""

    status, code, title = 409, "plan_job_target_mismatch", "Conflict"


def claim_key(platform_kind: str, address: str) -> str:
    """The claim key of a normalized target address, per platform (the one place it is made)."""
    if platform_kind == "snowflake":
        # TODO(#300): use #300's one-key-per-Snowflake-account derivation here once it is on main.
        return address_key(address)
    return address_key(address)


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
            bindings=s.BINDINGS[row["platform_kind"]].model_validate(row["bindings"]),
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
            address = normalize_address(body.platform_kind, body.address, self.host_suffixes)
        except AddressRefused as exc:
            raise ValidationFailed(
                str(exc),
                errors=[{"loc": ["body", "address"], "msg": str(exc), "type": "address"}],
            ) from exc
        session = self.sc.session
        # B-2524: the definer answers None, naming nobody, when another organization holds it
        claim_id = repository.claim_platform(
            session, body.platform_kind, claim_key(body.platform_kind, address)
        )
        if claim_id is None:
            session.rollback()
            raise PlatformAlreadyRegistered
        if isinstance(body.bindings, s.SnowflakeBindings):
            # the claim's lock serializes this check with every register and archive on it
            repository.lock_claim(session, claim_id, tenant_id=self.sc.tenant_id)
            if repository.snowflake_binding_in_use(
                session,
                claim_id,
                body.bindings.package,
                body.bindings.application,
                tenant_id=self.sc.tenant_id,
            ):
                session.rollback()
                raise BindingInUse
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

    def plannable(self, target_id: UUID) -> Mapping[str, Any]:
        """The target, if the caller may request a plan on it and its organization, workspace
        and project are active (an unreadable target is `target not found`)."""
        row = self._get_row(target_id)
        try:
            self.sc.pdp.require(
                "delivery:plan:request", self._ref(row), hide_unless="delivery:target:read"
            )
        except NotFound as exc:
            raise NotFound("target not found") from exc
        ws_map, pr_map = tenancy.scope_slug_map(
            self.sc, [row["workspace_id"]], [row["project_id"]] if row["project_id"] else []
        )
        self._active_scope(
            ws_map[row["workspace_id"]], pr_map[row["project_id"]] if row["project_id"] else None
        )
        return row

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


# ====================================================================== executors (B-2664)
EXECUTOR_TOKEN = re.compile(r"^shx_([0-9A-Za-z]{8})_([0-9A-Za-z]{43})$")
EXECUTOR_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
CREDENTIAL_LIFETIME = timedelta(days=90)
MAX_LIVE_CREDENTIALS = 2
PREFIX_CONSTRAINT = "uq_delivery_executor_credentials__tenant_prefix"
ONE_ACTIVE_CONSTRAINT = "uq_delivery_executors__one_active"
ONE_ACTIVE = "the organization already has an active executor"
ISSUE_REFUSED = "the credential could not be issued"


def _constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def _credential_status(row: Mapping[str, Any]) -> Literal["live", "revoked", "expired"]:
    if row["revoked_at"] is not None:
        return "revoked"
    return "live" if row["expires_at"] > utcnow() else "expired"


def _executor_out(row: Mapping[str, Any]) -> s.DeliveryExecutorOut:
    return s.DeliveryExecutorOut(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        last_seen_at=row["last_seen_at"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _credential_out(row: Mapping[str, Any]) -> s.CredentialOut:
    return s.CredentialOut(
        id=row["id"],
        executor_id=row["executor_id"],
        prefix=row["prefix"],
        status=_credential_status(row),
        created_by=row["created_by"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


class ExecutorService:
    """The organization's executor identity and its credentials (design 5, section 9 items 4, 12
    and 15). The secret of a credential exists only in the issue/rotate response."""

    def __init__(self, scope: TenantScope, pepper: bytes) -> None:
        self.sc = scope
        self.pepper = pepper

    # ================================================================== helpers
    def _ref(self, executor_id: UUID) -> ResourceRef:
        return ResourceRef(
            self.sc.tenant_id, resource_type="delivery_executor", resource_id=executor_id
        )

    def _get_row(self, executor_id: UUID, *, for_update: bool = False) -> Mapping[str, Any]:
        row = repository.get_executor(
            self.sc.session, executor_id, tenant_id=self.sc.tenant_id, for_update=for_update
        )
        if row is None or row["tenant_id"] != self.sc.tenant_id:
            raise NotFound("executor not found")
        return row

    def _require(self, action: str, executor_id: UUID) -> None:
        try:
            self.sc.pdp.require(
                action, self._ref(executor_id), hide_unless="delivery:executor:read"
            )
        except NotFound as exc:
            # an executor the caller cannot read gets the answer an unknown id gets
            raise NotFound("executor not found") from exc

    def _readable(self, executor_id: UUID) -> Mapping[str, Any]:
        row = self._get_row(executor_id)
        self._require("delivery:executor:read", executor_id)
        return row

    def _manageable(self, executor_id: UUID) -> Mapping[str, Any]:
        row = self._get_row(executor_id)
        self._require("delivery:executor:manage", executor_id)
        self._active_org()
        return row

    def _credential(self, executor_id: UUID, credential_id: UUID) -> Mapping[str, Any]:
        row = repository.get_credential(
            self.sc.session, credential_id, executor_id, tenant_id=self.sc.tenant_id
        )
        if row is None or row["tenant_id"] != self.sc.tenant_id:
            raise NotFound("credential not found")
        return row

    def _active_org(self) -> None:
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")

    def _audit(
        self,
        action: str,
        executor: Mapping[str, Any],
        details: Mapping[str, Any],
        credential_id: UUID | None = None,
    ) -> None:
        audit.record(
            self.sc.session,
            self.sc.ctx,
            action=action,
            tenant_id=self.sc.tenant_id,
            target_type="delivery_executor_credential" if credential_id else "delivery_executor",
            target_id=credential_id or executor["id"],
            target_label=f"delivery_executor:{executor['name']}",
            details={"executor_id": str(executor["id"]), "name": executor["name"], **details},
        )

    def _issue(self, executor_id: UUID) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
        """Lock the executor, check it may take a credential, insert one: (executor, credential
        row, full token). Only a prefix collision is retried; any other refusal is `Conflict`."""
        session = self.sc.session
        executor = self._get_row(executor_id, for_update=True)
        if executor["status"] != "active":
            raise Conflict("the executor is disabled")
        live = repository.count_live_credentials(session, executor_id, tenant_id=self.sc.tenant_id)
        if live >= MAX_LIVE_CREDENTIALS:
            raise Conflict("the executor already has two live credentials")
        for _ in range(apikeys.MINT_ATTEMPTS):
            prefix = "".join(secrets.choice(apikeys.ALPHABET) for _ in range(apikeys.PREFIX_LEN))
            secret = "".join(secrets.choice(apikeys.ALPHABET) for _ in range(apikeys.SECRET_LEN))
            full = f"shx_{prefix}_{secret}"
            credential_id = uuid7()
            try:
                with session.begin_nested():
                    repository.insert_credential(
                        session,
                        {
                            "id": credential_id,
                            "tenant_id": self.sc.tenant_id,
                            "executor_id": executor_id,
                            "prefix": prefix,
                            "secret_hash": apikeys.hash_key(self.pepper, full),
                            "created_by": self.sc.principal.principal_id,
                            "expires_at": utcnow() + CREDENTIAL_LIFETIME,
                        },
                    )
            except IntegrityError as exc:
                if _constraint(exc) == PREFIX_CONSTRAINT:
                    continue  # astronomically rare: draw another prefix
                raise Conflict(ISSUE_REFUSED) from None
            register_secret(full)
            return executor, self._credential(executor_id, credential_id), full
        raise Conflict(ISSUE_REFUSED)

    # ================================================================== routes
    def list_executors(
        self, *, status: str | None, limit: int, cursor: str | None
    ) -> Page[s.DeliveryExecutorOut]:
        self.sc.pdp.require("delivery:executor:read", self.sc.tenant_ref)
        rows = repository.list_executors(
            self.sc.session,
            tenant_id=self.sc.tenant_id,
            status=status,
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[_executor_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, executor_id: UUID) -> s.DeliveryExecutorOut:
        return _executor_out(self._readable(executor_id))

    def create(self, body: s.ExecutorCreate) -> s.DeliveryExecutorOut:
        self.sc.pdp.require("delivery:executor:manage", self.sc.tenant_ref)
        self._active_org()
        session = self.sc.session
        executor_id = uuid7()
        try:
            with session.begin_nested():
                repository.insert_executor(
                    session,
                    {
                        "id": executor_id,
                        "tenant_id": self.sc.tenant_id,
                        "name": body.name,
                        "created_by": self.sc.principal.principal_id,
                    },
                )
        except IntegrityError as exc:
            if _constraint(exc) == ONE_ACTIVE_CONSTRAINT:
                raise Conflict(ONE_ACTIVE) from None
            raise
        row = self._get_row(executor_id)
        self._audit("delivery.executor.created", row, {})
        session.commit()
        return _executor_out(row)

    def set_status(self, executor_id: UUID, body: s.ExecutorPatch) -> s.DeliveryExecutorOut:
        row = self._manageable(executor_id)
        if row["status"] == body.status:
            return _executor_out(row)  # no change, no audit row
        session = self.sc.session
        try:
            with session.begin_nested():
                updated = repository.set_executor_status(
                    session, executor_id, body.status, tenant_id=self.sc.tenant_id
                )
        except IntegrityError as exc:
            if _constraint(exc) == ONE_ACTIVE_CONSTRAINT:
                raise Conflict(ONE_ACTIVE) from None
            raise
        if updated is None:  # pragma: no cover - the row was read in this transaction
            raise NotFound("executor not found")
        if body.status == "disabled":
            revoked = repository.revoke_executor_credentials(
                session, executor_id, tenant_id=self.sc.tenant_id
            )
            self._audit(
                "delivery.executor.disabled",
                updated,
                {"revoked_credential_ids": [str(r["id"]) for r in revoked]},
            )
        else:
            self._audit("delivery.executor.enabled", updated, {})
        session.commit()
        return _executor_out(updated)

    def list_credentials(self, executor_id: UUID) -> list[s.CredentialOut]:
        self._readable(executor_id)
        rows = repository.list_credentials(
            self.sc.session, executor_id, tenant_id=self.sc.tenant_id
        )
        return [_credential_out(r) for r in rows]

    def issue_credential(self, executor_id: UUID) -> s.CredentialIssued:
        self._manageable(executor_id)
        executor, row, full = self._issue(executor_id)
        self._audit(
            "delivery.executor.credential.issued",
            executor,
            {"credential_id": str(row["id"]), "prefix": row["prefix"]},
            credential_id=row["id"],
        )
        self.sc.session.commit()
        return s.CredentialIssued(credential=_credential_out(row), secret=full)

    def rotate_credential(self, executor_id: UUID, credential_id: UUID) -> s.CredentialIssued:
        self._manageable(executor_id)
        old = self._credential(executor_id, credential_id)
        if _credential_status(old) != "live":
            raise Conflict("only a live credential can be rotated")
        executor, row, full = self._issue(executor_id)
        self._audit(
            "delivery.executor.credential.rotated",
            executor,
            {
                "old_credential_id": str(old["id"]),
                "old_prefix": old["prefix"],
                "new_credential_id": str(row["id"]),
                "new_prefix": row["prefix"],
            },
            credential_id=row["id"],
        )
        self.sc.session.commit()
        return s.CredentialIssued(credential=_credential_out(row), secret=full)

    def revoke_credential(self, executor_id: UUID, credential_id: UUID) -> s.CredentialOut:
        executor = self._manageable(executor_id)
        self._credential(executor_id, credential_id)
        session = self.sc.session
        row = repository.revoke_credential(session, credential_id, tenant_id=self.sc.tenant_id)
        if row is None:
            raise Conflict("the credential is already revoked")
        self._audit(
            "delivery.executor.credential.revoked",
            executor,
            {"credential_id": str(row["id"]), "prefix": row["prefix"]},
            credential_id=row["id"],
        )
        session.commit()
        return _credential_out(row)


@dataclass(frozen=True)
class AuthenticatedExecutor:
    tenant_id: UUID
    executor_id: UUID
    name: str
    status: str
    version: str | None
    last_seen_at: datetime | None


def _reject_executor_token(session: Session, ctx: RequestContext) -> None:
    """A refused executor token proves no organization: one installation-level row with the
    client address and no token material, committed before the 401 (B-2730, as B-917)."""
    db.set_principal(session, None)
    db.set_tenant(session, None)
    audit.record(
        session,
        ctx,
        action=EXECUTOR_TOKEN_REJECTED,
        outcome="failure",
        tenant_id=None,
        auth_method="executor_token",
        details={"reason": "invalid"},
    )
    session.commit()


def authenticate_executor(
    session: Session,
    ctx: RequestContext,
    peppers: Iterable[bytes],
    token: str | None,
    version: str | None,
) -> AuthenticatedExecutor:
    """Authenticate an `shx_` executor token (design 5; section 9 items 12, 13).

    Every refusal (no or malformed token, unknown prefix, wrong secret, a revoked or expired
    credential, a disabled executor, an organization that is not active) is the same 401
    `invalid_credentials`, and writes one installation-level `delivery.executor.token.rejected`
    audit row (reason `invalid`, the client address as `actor_ip`, never the token, its prefix or
    its hash; B-2730). The per-source limit on failures is applied by the caller
    (`gateway_api.require_executor`). On success the session runs in the executor's organization
    (no principal), and `last_seen_at` (and `version`, when reported) are updated under RLS."""
    match = EXECUTOR_TOKEN.match(token or "")
    resolved = None
    if match is not None:
        for pepper in peppers:  # current pepper first, then the retired ones
            resolved = repository.resolve_executor(
                session, match.group(1), apikeys.hash_key(pepper, match.group(0))
            )
            if resolved is not None:
                break
    if resolved is None:
        _reject_executor_token(session, ctx)
        raise InvalidCredentials("invalid credentials")
    if version is not None and not EXECUTOR_VERSION.fullmatch(version):
        raise ValidationFailed(
            "invalid X-Executor-Version",
            errors=[
                {
                    "loc": ["header", "x-executor-version"],
                    "msg": "must match ^[A-Za-z0-9._+-]{1,64}$",
                    "type": "pattern",
                }
            ],
        )
    tenant_id, executor_id = resolved["tenant_id"], resolved["executor_id"]
    db.set_principal(session, None)
    db.set_tenant(session, tenant_id)
    row = repository.touch_executor(session, executor_id, version, tenant_id=tenant_id)
    if row is None:  # pragma: no cover - the resolver just found it active
        raise InvalidCredentials("invalid credentials")
    session.commit()
    return AuthenticatedExecutor(
        tenant_id=tenant_id,
        executor_id=executor_id,
        name=row["name"],
        status=row["status"],
        version=row["version"],
        last_seen_at=row["last_seen_at"],
    )


# ====================================================================== plan jobs (B-2675..B-2679)
def plan_digest(
    *,
    artifact_digest: str,
    target_revision: int,
    snapshot_fingerprint: str,
    steps: Sequence[Mapping[str, Any]],
    adapter_version: str,
    toolchain_digest: str,
    release_digest: str | None,
) -> str:
    """The sealed plan's digest (design 4): artifact digest, target revision, snapshot
    fingerprint, steps, adapter version, toolchain digest and release digest (SP3 section 6)."""
    return digest(
        {
            "artifact_digest": artifact_digest,
            "target_revision": target_revision,
            "snapshot_fingerprint": snapshot_fingerprint,
            "steps": steps,
            "adapter_version": adapter_version,
            "toolchain_digest": toolchain_digest,
            "release_digest": release_digest,
        }
    )


def start_planning(session: Session, job: Mapping[str, Any]) -> None:
    """A leased `plan` job moves its `requested` plan to `planning` (same transaction)."""
    if job["kind"] == "plan" and job["plan_id"] is not None:
        repository.start_planning(session, job["plan_id"], tenant_id=job["tenant_id"])


def settle_plans(session: Session, tenant_id: UUID) -> None:
    """Plans whose plan job was lost fail; sealed plans at or past `expires_at` expire."""
    repository.settle_plans(session, tenant_id=tenant_id)


def on_plan_result(session: Session, job: Mapping[str, Any], body: JobResultSubmission) -> None:
    """The `plan` kind's result hook: fail the plan or seal it. A plan no longer `requested` or
    `planning` is left as it is."""
    tenant_id, plan_id = job["tenant_id"], job["plan_id"]
    if plan_id is None:
        return
    plan = repository.get_plan(session, plan_id, tenant_id=tenant_id, for_update=True)
    if plan is None or plan["status"] not in ("requested", "planning"):
        return
    if body.status == "failed":
        repository.fail_plan(
            session, plan_id, f"plan job failed: {body.error_code}", tenant_id=tenant_id
        )
        return
    value = PlanJobResult.model_validate(body.value)
    target = repository.get_target(session, plan["target_id"], tenant_id=tenant_id)
    if target is None or target["status"] != "active":
        repository.fail_plan(
            session, plan_id, "the target is no longer active", tenant_id=tenant_id
        )
        return
    if target["revision"] != plan["target_revision"]:
        repository.fail_plan(session, plan_id, "the target changed", tenant_id=tenant_id)
        return
    try:
        planning.validate_changes(list(value.steps))
    except (ValueError, PermissionError) as exc:
        repository.fail_plan(session, plan_id, str(exc), tenant_id=tenant_id)
        return
    for resource in value.snapshot.resources:  # settled call 18: no adoption
        owner = repository.native_owner(
            session,
            plan["target_id"],
            resource.resource_type,
            resource.native_id,
            tenant_id=tenant_id,
        )
        if owner != plan["application_key"]:
            repository.fail_plan(
                session, plan_id, "refusing to adopt an unowned native app", tenant_id=tenant_id
            )
            return
    steps = [step.model_dump(mode="json") for step in value.steps]
    repository.seal_plan(
        session,
        plan_id,
        {
            "steps": steps,
            "snapshot": value.snapshot.model_dump(mode="json"),
            "adapter_version": value.adapter_version,
            "toolchain_digest": value.toolchain_digest,
            "digest": plan_digest(
                artifact_digest=plan["artifact_digest"],
                target_revision=plan["target_revision"],
                snapshot_fingerprint=value.snapshot.fingerprint,
                steps=steps,
                adapter_version=value.adapter_version,
                toolchain_digest=value.toolchain_digest,
                release_digest=plan["release_digest"],
            ),
        },
        tenant_id=tenant_id,
    )


# ====================================================================== jobs and deployments
READ_ONLY_MAX_ATTEMPTS = 4  # one lease plus up to 3 re-queues (settled call 8)
APPLY_MAX_ATTEMPTS = 1  # a platform write is never retried
POLL_DELAY = timedelta(seconds=15)  # settled call 5: the next observe/verify job
REQUIRED_CHECKS = ("native-runtime", "runtime-warehouse")


def enqueue_job(
    session: Session,
    *,
    tenant_id: UUID,
    kind: str,
    plan_id: UUID | None = None,
    deployment_id: UUID | None = None,
    target_id: UUID | None = None,
    run_after: datetime | None = None,
    deadline: datetime | None = None,
) -> UUID:
    """Queue a job (the caller commits); `apply` gets one attempt, other kinds four. A `plan` job
    naming a plan must name a plan of the same organization and that plan's target (B-2732)."""
    if kind == "plan" and plan_id is not None:
        plan = repository.get_plan(session, plan_id, tenant_id=tenant_id)
        if plan is None:
            raise NotFound("plan not found")
        if plan["target_id"] != target_id:
            raise PlanJobTargetMismatch("plan job target does not match its plan")
    job_id = uuid7()
    repository.insert_job(
        session,
        {
            "tenant_id": tenant_id,
            "id": job_id,
            "kind": kind,
            "plan_id": plan_id,
            "deployment_id": deployment_id,
            "target_id": target_id,
            "run_after": run_after,
            "deadline": deadline,
            "max_attempts": APPLY_MAX_ATTEMPTS if kind == "apply" else READ_ONLY_MAX_ATTEMPTS,
        },
    )
    return job_id


def settle_deployments(session: Session, tenant_id: UUID) -> None:
    """The deployment settle step (B-2683), run after the lost-job sweep: lost applies become
    `unknown_outcome` or `failed`, unleased applies past the plan's expiry `expired`, lost or
    overdue observe/verify work `failed`, and the queued applies of a target with an unresolved
    unknown outcome are held. Each lost apply's move to `unknown_outcome` is audited."""
    for moved in repository.settle_deployments(session, tenant_id=tenant_id):
        row = repository.get_deployment(session, moved["id"], tenant_id=tenant_id)
        if row is not None:
            _audit_unknown_outcome(
                session, row, moved["status_reason"], "executor_lost", moved["job_id"]
            )


def start_deployment(session: Session, job: Mapping[str, Any]) -> None:
    """A leased `apply` job moves its `queued` deployment to `leased` (same transaction)."""
    if job["kind"] == "apply" and job["deployment_id"] is not None:
        repository.set_deployment_status(
            session,
            job["deployment_id"],
            "leased",
            None,
            expected=("queued",),
            tenant_id=job["tenant_id"],
        )


def start_applying(session: Session, job: Mapping[str, Any]) -> None:
    """An accepted `intent` of an `apply` job moves a `leased` deployment to `applying`."""
    if job["kind"] == "apply" and job["deployment_id"] is not None:
        repository.set_deployment_status(
            session,
            job["deployment_id"],
            "applying",
            None,
            expected=("leased",),
            tenant_id=job["tenant_id"],
        )


def _deployment(
    session: Session, job: Mapping[str, Any], expected: Sequence[str]
) -> Mapping[str, Any] | None:
    """The job's deployment, locked, if it is in one of `expected` (else the hook does nothing)."""
    if job["deployment_id"] is None:
        return None
    row = repository.get_deployment(
        session, job["deployment_id"], tenant_id=job["tenant_id"], for_update=True
    )
    return row if row is not None and row["status"] in expected else None


def _fail(session: Session, row: Mapping[str, Any], reason: str, *, status: str = "failed") -> None:
    repository.set_deployment_status(
        session, row["id"], status, reason, expected=(row["status"],), tenant_id=row["tenant_id"]
    )


def _audit_unknown_outcome(
    session: Session, row: Mapping[str, Any], reason: str, source: str, job_id: UUID
) -> None:
    """One `delivery.deployment.unknown_outcome` row for the deployment's move (B-2736)."""
    audit.record(
        session,
        UNKNOWN_OUTCOME_CONTEXT,
        action=DEPLOYMENT_UNKNOWN_OUTCOME,
        tenant_id=row["tenant_id"],
        target_type="delivery_deployment",
        target_id=row["id"],
        target_label=f"delivery_deployment:{row['application_key']}",
        workspace_id=row["workspace_id"],
        project_id=row["project_id"],
        details={
            "deployment_id": str(row["id"]),
            "plan_id": str(row["plan_id"]),
            "target_id": str(row["target_id"]),
            "application_key": row["application_key"],
            "reason": reason,
            "source": source,
            "job_id": str(job_id),
        },
    )


def _unknown_outcome(
    session: Session, job: Mapping[str, Any], row: Mapping[str, Any], reason: str
) -> None:
    """Move the locked deployment to `unknown_outcome` and audit it."""
    _fail(session, row, reason, status="unknown_outcome")
    _audit_unknown_outcome(session, row, reason, "apply_result", job["id"])


def _poll(
    session: Session, job: Mapping[str, Any], row: Mapping[str, Any], kind: str, deadline: datetime
) -> None:
    """Queue the deployment's next `observe` or `verify` job, due 15 s from now."""
    enqueue_job(
        session,
        tenant_id=row["tenant_id"],
        kind=kind,
        plan_id=row["plan_id"],
        deployment_id=row["id"],
        target_id=row["target_id"],
        run_after=job["db_now"] + POLL_DELAY,
        deadline=deadline,
    )


def _receipt_refused(field: str, message: str) -> ValidationFailed:
    return ValidationFailed(
        "result value is invalid",
        errors=[{"loc": ["body", "value", field], "msg": message, "type": "value_error"}],
    )


def on_apply_result(session: Session, job: Mapping[str, Any], body: JobResultSubmission) -> None:
    """The `apply` kind's result hook (B-2684). A receipt for another organization, target or
    artifact is refused (422, nothing changes). A succeeded apply with an unfinished step is an
    unknown outcome; otherwise ownership is recorded and observing starts. A failed apply after
    any journal step is an unknown outcome, else `failed`."""
    tenant_id = job["tenant_id"]
    receipt = None
    if body.status == "succeeded":
        receipt = ApplyReceipt.model_validate(body.value)
        plan = (
            repository.get_plan(session, job["plan_id"], tenant_id=tenant_id)
            if job["plan_id"] is not None
            else None
        )
        if receipt.tenant_id != tenant_id:
            raise _receipt_refused("tenant_id", "the receipt names another organization")
        if receipt.target_id != job["target_id"]:
            raise _receipt_refused("target_id", "the receipt names another target")
        if plan is None or receipt.artifact_digest != plan["artifact_digest"]:
            raise _receipt_refused("artifact_digest", "the receipt names another artifact")
        if any(r.target_id != job["target_id"] for r in receipt.resources):
            raise _receipt_refused("resources", "a resource names another target")
    row = _deployment(session, job, ("leased", "applying"))
    if row is None:
        return
    any_step, open_intent = repository.journal_state(session, row["id"], tenant_id=tenant_id)
    if receipt is None:
        if any_step:
            _unknown_outcome(
                session,
                job,
                row,
                f"apply failed after a platform write may have happened: {body.error_code}",
            )
        else:
            _fail(session, row, "no platform write was attempted")
        return
    if open_intent:
        _unknown_outcome(session, job, row, "a platform write did not complete")
        return
    for resource in receipt.resources:
        owner = repository.native_owner(
            session,
            row["target_id"],
            resource.resource_type,
            resource.native_id,
            tenant_id=tenant_id,
        )
        if owner is not None and owner != row["application_key"]:
            _fail(session, row, "resource owned by another application")
            return
    for resource in receipt.resources:
        repository.insert_native_resource(
            session,
            {
                "tenant_id": tenant_id,
                "target_id": row["target_id"],
                "application_key": row["application_key"],
                "resource_type": resource.resource_type,
                "native_id": resource.native_id,
                "deployment_id": row["id"],
            },
        )
    deadline = repository.start_observing(
        session, row["id"], receipt.model_dump(mode="json"), tenant_id=tenant_id
    )
    _poll(session, job, row, "observe", deadline)


def on_observe_result(session: Session, job: Mapping[str, Any], body: JobResultSubmission) -> None:
    """The `observe` kind's result hook: `succeeded` starts verifying, `running`/`unknown` polls
    again until the observe deadline, `failed` (or a failed job) fails the deployment."""
    row = _deployment(session, job, ("observing",))
    if row is None:
        return
    if body.status == "failed":
        _fail(session, row, f"observe failed: {body.error_code}")
        return
    observation = NativeObservation.model_validate(body.value)
    if observation.state == "succeeded":
        repository.set_deployment_status(
            session,
            row["id"],
            "verifying",
            None,
            expected=("observing",),
            tenant_id=row["tenant_id"],
        )
        _poll(session, job, row, "verify", row["observe_deadline"])
    elif observation.state == "failed":
        _fail(session, row, "the platform reported a failure")
    elif row["db_now"] < row["observe_deadline"]:
        _poll(session, job, row, "observe", row["observe_deadline"])
    else:
        _fail(session, row, "did not become healthy")


def on_verify_result(session: Session, job: Mapping[str, Any], body: JobResultSubmission) -> None:
    """The `verify` kind's result hook: `verified` only when `native-runtime` and
    `runtime-warehouse` both passed, otherwise `failed`."""
    row = _deployment(session, job, ("verifying",))
    if row is None:
        return
    if body.status == "failed":
        _fail(session, row, "verification failed")
        return
    report = VerificationReport.model_validate(body.value)
    repository.set_verification(
        session, row["id"], report.model_dump(mode="json"), tenant_id=row["tenant_id"]
    )
    passed = {check.name for check in report.checks if check.status == "passed"}
    if all(name in passed for name in REQUIRED_CHECKS):
        repository.set_deployment_status(
            session,
            row["id"],
            "verified",
            None,
            expected=("verifying",),
            tenant_id=row["tenant_id"],
        )
    else:
        _fail(session, row, "verification failed")


def on_reconcile_result(
    session: Session, job: Mapping[str, Any], body: JobResultSubmission
) -> None:
    """The `reconcile` kind's result hook: store the evidence; the status does not change
    (settled call 11)."""
    row = _deployment(session, job, ("unknown_outcome",))
    if row is None:
        return
    evidence: dict[str, Any]
    if body.status == "failed":
        evidence = {"status": "failed", "error_code": body.error_code, "job_id": str(job["id"])}
    else:
        result = ReconciliationResult.model_validate(body.value).model_dump(mode="json")
        evidence = {
            "state": result["state"],
            "evidence": result["evidence"],
            "resources": result["resources"],
            "job_id": str(job["id"]),
        }
    repository.set_reconcile_evidence(session, row["id"], evidence, tenant_id=row["tenant_id"])
