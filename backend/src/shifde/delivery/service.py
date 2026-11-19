"""Deployment targets and the one-platform-per-organization rule (design sections 3 and 9,
DECISIONS B-2501, lead ruling B-2524).

A target's address is normalized and allow-listed by `adapters.native.addresses` before anything
is written (this module makes no outbound request). The organization's claim on the address comes
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
from shifde.adapters.native.addresses import (
    AddressRefused,
    address_key,
    normalize_databricks_address,
)
from shifde.adapters.native.contracts import JobResultSubmission, PlanJobResult, digest
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.core import db
from shifde.core.clock import utcnow
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
        # B-2524: the definer answers None, naming nobody, when another organization holds it
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


def authenticate_executor(
    session: Session, peppers: Iterable[bytes], token: str | None, version: str | None
) -> AuthenticatedExecutor:
    """Authenticate an `shx_` executor token (design 5; section 9 items 12, 13).

    Every refusal (no or malformed token, unknown prefix, wrong secret, a revoked or expired
    credential, a disabled executor, an organization that is not active) is the same 401
    `invalid_credentials`. On success the session runs in the executor's organization (no
    principal), and `last_seen_at` (and `version`, when reported) are updated under RLS."""
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
) -> str:
    """The sealed plan's digest (design 4): artifact digest, target revision, snapshot
    fingerprint, steps, adapter version and toolchain digest."""
    return digest(
        {
            "artifact_digest": artifact_digest,
            "target_revision": target_revision,
            "snapshot_fingerprint": snapshot_fingerprint,
            "steps": steps,
            "adapter_version": adapter_version,
            "toolchain_digest": toolchain_digest,
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
            ),
        },
        tenant_id=tenant_id,
    )
