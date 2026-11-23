"""Connections: governed configuration, testing, discovery and use of external systems.

Every row is read and written for the caller's resolved client only (`TenantScope.tenant_id`, from
`tenancy.dependencies.resolve_org`, never a path or header value): the repository keys each call
by `tenant_id` as well as row-level security (DECISIONS B-1503, B-1505).

``ConnectionService.open_for_use`` is the server-side path for later phases (pipelines, tools,
ontology): PDP ``connection:use`` (resource-level bindings honoured) -> status -> access mode ->
egress policy -> secret resolution through the P2 secret store -> audit; it yields an adapter
session. Secrets are resolved only here, only as late as possible, and never leave the adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal  # Any: connection rows / adapter JSON payloads
from uuid import UUID

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.base import (
    AdapterDescriptor,
    AdapterFailure,
    AdapterRules,
    ConfigRefused,
    ConnectionConfig,
    ConnectionSession,
    Credential,
    ErrorClass,
    ExecContext,
)
from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.connections import egress, repository
from shifde.connections import schemas as s
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.db import Database
from shifde.core.errors import (
    AdapterNotImplemented,
    AppError,
    Conflict,
    ConnectionDisabled,
    EgressDenied,
    Forbidden,
    NotFound,
    RateLimited,
    ScopeArchived,
    SecretScopeMismatch,
    UpstreamError,
    UseModelsEndpoint,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.core.redaction import scrub, scrub_text
from shifde.secrets import service as secrets_service
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope
from shifde.vendors import service as vendors

USABLE_STATUSES = frozenset({"active"})  # contract section 4: degraded is denied
TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"active", "disabled", "retired"}),
    "active": frozenset({"disabled", "retired"}),
    "degraded": frozenset({"active", "disabled", "retired"}),
    "disabled": frozenset({"active", "retired"}),
    "retired": frozenset(),
}
CONFIG_AFFECTING = frozenset(
    {"config", "auth_method", "secret_ref_id", "egress_policy_id", "access_mode", "vendor_id"}
)
TEST_VALIDITY = timedelta(hours=24)
TEST_JOB_KIND = "connection.test"  # `shifde.jobs.kind` of a queued connection test (B-2610)
ERROR_CODE = re.compile(r"[a-z][a-z_]*")  # an `AppError.code` the worker stored as `last_error`
SCOPE_MISMATCH = "the secret must have the connection's scope"
# the kind of a connection's own secret (`connection-<id>`) per auth method (rule 4.7, B-2747)
CREDENTIAL_KINDS: dict[str, str] = {
    "bearer": "token",
    "oauth2_client_credentials": "token",
    "key_pair": "pem",
    "mtls": "pem",
    "api_key": "api_key",
    "basic": "password",
}
NO_SECRET_FOR_NONE = "a secret cannot be attached when auth_method is 'none'"


def integrity_error(exc: IntegrityError) -> AppError | None:
    """HTTP mapping of a database refusal by SQLSTATE (#156 rule 4.5, B-2745): a duplicate name
    in one scope is 409, a `connections_guard` refusal is 422 with the guard's own message (the
    RAISE text, never ids or values); None for anything else, which the caller re-raises."""
    state = getattr(exc.orig, "sqlstate", None)
    if state == "23505":
        return Conflict("a connection with this name already exists in this scope")
    if state == "23514":
        diag = getattr(exc.orig, "diag", None)
        return SecretScopeMismatch(getattr(diag, "message_primary", None) or SCOPE_MISMATCH)
    return None


def failure_to_error(exc: AdapterFailure) -> AppError:
    """HTTP mapping of a classified adapter failure."""
    ec = exc.error_class
    if ec in (ErrorClass.EGRESS, ErrorClass.DNS):
        return EgressDenied(exc.message)
    if ec in (ErrorClass.CONFIG, ErrorClass.CAPABILITY):
        return ValidationFailed(exc.message)
    if ec is ErrorClass.RATE_LIMIT:
        return RateLimited(exc.message)
    return UpstreamError(f"{ec.value}: {exc.message}")


@dataclass(frozen=True)
class ConnectionPlacement:
    """Where a connection sits (NULL workspace: organization-wide) and its adapter."""

    workspace_id: UUID | None
    project_id: UUID | None
    adapter_key: str


class ConnectionService:
    def __init__(
        self, scope: TenantScope, runtime: IntegrationRuntime, rules: AdapterRules | None = None
    ) -> None:
        self.sc = scope
        self.runtime = runtime
        # the installation rules `normalize_config` applies on create/update; without them the
        # built-in rules only (fail closed, B-2753)
        self.rules = rules or AdapterRules()

    # ================================================================== helpers
    @property
    def _session(self) -> Session:
        return self.sc.session

    def ref_of(self, row: Mapping[str, Any]) -> ResourceRef:
        return ResourceRef(
            self.sc.tenant_id,
            row["workspace_id"],
            row["project_id"],
            resource_type="connection",
            resource_id=row["id"],
        )

    def _get_row(self, connection_id: UUID) -> Mapping[str, Any]:
        row = repository.get_connection(self._session, connection_id, tenant_id=self.sc.tenant_id)
        if row is None or row["tenant_id"] != self.sc.tenant_id:
            raise NotFound("connection not found")
        return row

    def _own(self, row: Mapping[str, Any]) -> None:
        """A row handed in by another module (`open_trusted`, `prepare_call`) must be this
        client's: its secret, egress policies and execution context are resolved for its
        `tenant_id`, which must be the caller's client (an unknown id gets the same answer)."""
        if row["tenant_id"] != self.sc.tenant_id:
            raise NotFound("connection not found")

    def _require(
        self, action: str, row: Mapping[str, Any], *, hide: str | None = "connection:read"
    ) -> None:
        try:
            self.sc.pdp.require(action, self.ref_of(row), hide_unless=hide)
        except NotFound as exc:
            # a connection the caller cannot read (another workspace of the same client) gets the
            # answer an unknown id gets, on every route (connections review 2 item 2, B-1512)
            raise NotFound("connection not found") from exc

    @staticmethod
    def _checked_version(sent: int | None, row: Mapping[str, Any]) -> int:
        """The version the write is conditional on: always the one this request read, because
        every check (host change and `secret:use`, read-only confirmation, activation freshness)
        runs on that row. A caller's `row_version` that differs from it is refused here, before any
        check; a concurrent change committed after the read makes the conditional write miss, so
        both answer 409 instead of writing a decision taken on another row (connections review 2
        item 1, isolation review 3; B-1511, B-1515)."""
        read = int(row["row_version"])
        if sent is not None and sent != read:
            raise Conflict("row_version mismatch: the connection was modified; reload and retry")
        return read

    def _active_tenant(self) -> None:
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")

    def _descriptor(self, key: str) -> AdapterDescriptor:
        desc = self.runtime.registry.descriptor(key)
        if desc is None:
            raise ValidationFailed(f"unknown adapter: {key}")
        return desc

    def _audit(
        self,
        action: str,
        row: Mapping[str, Any],
        details: dict[str, object],
        outcome: str = "success",
    ) -> None:
        audit.record(
            self._session,
            self.sc.ctx,
            action=action,
            outcome=outcome,
            tenant_id=self.sc.tenant_id,
            target_type="connection",
            target_id=row["id"],
            target_label=f"connection:{row['name']}",
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            details=details,
        )

    def _audit_secret(
        self,
        action: str,
        sec: secrets_service.SecretMeta,
        details: dict[str, object],
        row: Mapping[str, Any],
    ) -> None:
        """A `secret.*` event of the connection `row`'s own credential (B-2752): the admin
        route's target and details, with the connection's workspace and project."""
        audit.record(
            self._session,
            self.sc.ctx,
            action=action,
            tenant_id=self.sc.tenant_id,
            target_type="secret",
            target_id=sec.id,
            target_label=f"secret:{sec.name}",
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            details=details,
        )

    def _commit(self) -> None:
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            mapped = integrity_error(exc)
            if mapped is None:
                raise
            raise mapped from exc

    def _out(
        self,
        row: Mapping[str, Any],
        slugs: tuple[dict[UUID, str], dict[UUID, str]] | None = None,
    ) -> s.ConnectionOut:
        ws_map, pr_map = slugs or tenancy.scope_slug_map(
            self.sc,
            [row["workspace_id"]] if row["workspace_id"] else [],
            [row["project_id"]] if row["project_id"] else [],
        )
        desc = self.runtime.registry.descriptor(row["adapter_key"])
        hidden = set(desc.secret_fields) if desc else set()
        config = {k: v for k, v in (row["config"] or {}).items() if k not in hidden}
        return s.ConnectionOut(
            id=row["id"],
            name=row["name"],
            scope=s.ScopeOut(
                workspace=ws_map.get(row["workspace_id"]) if row["workspace_id"] else None,
                project=pr_map.get(row["project_id"]) if row["project_id"] else None,
            ),
            connection_type=row["connection_type"],
            adapter_key=row["adapter_key"],
            vendor_id=row["vendor_id"],
            config=config,
            auth_method=row["auth_method"],
            secret_id=row["secret_ref_id"],
            secret_set=row["secret_ref_id"] is not None,
            egress_policy_id=row["egress_policy_id"],
            access_mode=row["access_mode"],
            status=row["status"],
            health_status=row["health_status"],
            health=row["health_status"],
            last_tested_at=row["last_tested_at"],
            last_test_outcome=row["last_test_outcome"],
            row_version=row["row_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ================================================================== validation
    def _validate_definition(
        self,
        *,
        connection_type: str,
        adapter_key: str,
        config: dict[str, Any],
        auth_method: str,
        secret_id: UUID | None,
        credential: bool = False,
    ) -> AdapterDescriptor:
        desc = self._descriptor(adapter_key)
        if desc.connection_type != connection_type:
            raise ValidationFailed(
                f"adapter {adapter_key!r} serves connection_type {desc.connection_type!r}, "
                f"not {connection_type!r}"
            )
        if auth_method not in desc.auth_methods:
            raise ValidationFailed(
                f"auth_method {auth_method!r} is not supported by {adapter_key!r} "
                f"(supported: {', '.join(desc.auth_methods)})"
            )
        if auth_method == "none" and (secret_id is not None or credential):
            raise ValidationFailed(NO_SECRET_FOR_NONE)
        misplaced = sorted(
            f
            for f, methods in desc.auth_only_fields.items()
            if f in config and auth_method not in methods
        )
        if misplaced:
            raise ValidationFailed(
                f"config fields {', '.join(misplaced)} do not apply to auth_method {auth_method!r}",
                errors=[
                    {
                        "loc": ["body", "config", f],
                        "msg": "only allowed with auth_method "
                        + " or ".join(desc.auth_only_fields[f]),
                        "type": "config",
                    }
                    for f in misplaced
                ],
            )
        adapter = self.runtime.registry.get(adapter_key)
        assert adapter is not None  # descriptor() found it above
        errors = adapter.validate_config(config)
        if errors:
            raise ValidationFailed(
                "connection config does not match the adapter schema",
                errors=[
                    {
                        "loc": ["body", "config", *e.path.split("/")],
                        "msg": e.message,
                        "type": "config",
                    }
                    for e in errors
                ],
            )
        return desc

    def _normalized(self, adapter_key: str, config: dict[str, Any]) -> dict[str, Any]:
        """The adapter's `normalize_config` of `config` (create, and PATCH with `config`; never on
        a read, test, discover or invoke path); unchanged when the adapter has no such hook."""
        normalize = getattr(self.runtime.registry.get(adapter_key), "normalize_config", None)
        if normalize is None:
            return config
        try:
            normalized: dict[str, Any] = normalize(config, self.rules)
        except ConfigRefused as exc:
            raise ValidationFailed(
                "connection config was refused",
                errors=[
                    {
                        "loc": ["body", "config", *e.path.split("/")],
                        "msg": e.message,
                        "type": "config",
                    }
                    for e in exc.errors
                ],
            ) from exc
        return normalized

    def _check_credential(
        self, adapter_key: str, auth_method: str, value: str, loc: list[str]
    ) -> None:
        """The adapter's `validate_credential` of an inline credential value (422 at `loc`; the
        messages are the adapter's fixed text, never the value)."""
        check = getattr(self.runtime.registry.get(adapter_key), "validate_credential", None)
        if check is None:
            return
        errors = check(auth_method, value)
        if errors:
            raise ValidationFailed(
                "the credential is not valid for this connection",
                errors=[{"loc": loc, "msg": e.message, "type": "credential"} for e in errors],
            )

    def _secret_ref(self, sec: secrets_service.SecretMeta) -> ResourceRef:
        if sec.scope_id is None:
            return self.sc.tenant_ref
        if sec.scope_type == "workspace":
            return ResourceRef(self.sc.tenant_id, workspace_id=sec.scope_id)
        return tenancy.resolve_scope_ref(self.sc, "project", sec.scope_id)

    def _check_secret(self, secret_id: UUID | None) -> secrets_service.SecretMeta | None:
        if secret_id is None:
            return None
        sec = secrets_service.get_secret(self._session, secret_id, tenant_id=self.sc.tenant_id)
        if sec is None or sec.tenant_id != self.sc.tenant_id or sec.status != "active":
            raise NotFound("secret not found")
        try:
            self.sc.pdp.require("secret:use", self._secret_ref(sec), hide_unless="secret:read")
        except NotFound as exc:  # hidden: the same answer as an id that does not exist (B-1508)
            raise NotFound("secret not found") from exc
        return sec

    @staticmethod
    def _check_secret_scope(
        sec: secrets_service.SecretMeta | None,
        workspace_id: UUID | None,
        project_id: UUID | None,
    ) -> None:
        """#156 rule 4.4 (B-2746): a project connection takes only a secret of its project, a
        workspace connection only one of its workspace, a client connection only a client one.
        Runs after `_check_secret`, so its 404/403 answers come first."""
        if sec is None:
            return
        if project_id is not None:
            ok = sec.scope_type == "project" and sec.scope_id == project_id
        elif workspace_id is not None:
            ok = sec.scope_type == "workspace" and sec.scope_id == workspace_id
        else:
            ok = sec.scope_type == "tenant"
        if not ok:
            raise SecretScopeMismatch(SCOPE_MISMATCH)

    def _destination_changed(
        self,
        descriptor: AdapterDescriptor,
        old: tuple[str, Mapping[str, Any]],
        new: tuple[str, Mapping[str, Any]],
    ) -> bool:
        """True when the credential's effective destination differs between the old and the new
        (auth_method, config): a destination field's value changed, or a change of auth method
        brings a destination field into effect or takes one out (to or from OAuth2's
        `oauth2_url`). A field that is missing on one side and present on the other counts."""
        return descriptor.destination(*old) != descriptor.destination(*new)

    def _require_secret_use_for_host_change(self, secret_id: UUID) -> None:
        """FR-CON-011: a credential must not follow a changed destination unless the caller could
        have used that credential themselves. Checked on the BOUND secret (the binding does not
        change), whether or not the caller can read it: the connection already names it, so a
        plain 403 (not a 404) is the honest answer. A disabled secret still counts: it may be
        re-enabled later and would then follow the new host."""
        sec = secrets_service.get_secret(self._session, secret_id, tenant_id=self.sc.tenant_id)
        if sec is None or sec.tenant_id != self.sc.tenant_id:
            raise NotFound("secret not found")
        try:
            self.sc.pdp.require("secret:use", self._secret_ref(sec))
        except Forbidden as exc:
            raise Forbidden(
                "changing where this connection sends its credential needs secret:use on the "
                "bound secret (or detach the secret in the same request)"
            ) from exc

    def _check_egress_policy(self, policy_id: UUID | None) -> None:
        if policy_id is None:
            return
        row = repository.get_egress_policy(self._session, policy_id, tenant_id=self.sc.tenant_id)
        if row is None or row["tenant_id"] != self.sc.tenant_id:
            raise NotFound("egress policy not found")
        self.sc.pdp.require("egress:manage", self.sc.tenant_ref)  # privileged choice

    def _active_scope(self, workspace: str | None, project: str | None) -> None:
        """Findings Item 12 (REQ F-02): an archived workspace or project takes no new connection
        (archived scopes are read-only, as for the tenancy objects themselves)."""
        if workspace is None:
            return
        if tenancy.get_workspace(self.sc, workspace).status != "active":
            raise ScopeArchived("workspace is archived")
        if project is not None and tenancy.get_project(self.sc, workspace, project).status != (
            "active"
        ):
            raise ScopeArchived("project is archived")

    @staticmethod
    def _require_read_only_confirmation(confirmed: bool | None) -> None:
        """Design 5.3 (DECISIONS B-1507): whoever binds a credential to a read connection
        confirms that the credential itself is read-only."""
        if confirmed is not True:
            raise ValidationFailed(
                "a read connection with a credential needs read_only_confirmed: true "
                "(confirm that the credential is read-only)",
                errors=[
                    {
                        "loc": ["body", "read_only_confirmed"],
                        "msg": "confirm that the credential is read-only",
                        "type": "confirmation",
                    }
                ],
            )

    def _create_owned_secret(
        self,
        connection_id: UUID,
        auth_method: str,
        workspace_id: UUID | None,
        project_id: UUID | None,
        value: str,
    ) -> secrets_service.SecretMeta:
        """The connection's own secret `connection-<id>`, in the connection's scope (B-2747)."""
        if project_id is not None:
            scope_type, scope_id = "project", project_id
        elif workspace_id is not None:
            scope_type, scope_id = "workspace", workspace_id
        else:
            scope_type, scope_id = "tenant", None
        return secrets_service.create_secret(
            self._session,
            self.runtime.settings.keyring,
            tenant_id=self.sc.tenant_id,
            name=f"connection-{connection_id}",
            value=value,
            kind=CREDENTIAL_KINDS[auth_method],
            scope_type=scope_type,
            scope_id=scope_id,
            created_by=self.sc.principal.principal_id,
        )

    def _check_vendor(self, vendor_id: UUID | None, *, current: UUID | None = None) -> None:
        """A connection may only NAME an active vendor. Keeping the vendor it already names is
        always allowed (existing objects keep working)."""
        if vendor_id is not None and vendor_id != current:
            vendors.require_active_vendor(self._session, self.sc.tenant_id, vendor_id)

    # ================================================================== CRUD
    def list(
        self,
        *,
        connection_type: str | None,
        status: str | None,
        workspace: str | None,
        project: str | None,
        include_shared: bool,
        limit: int,
        cursor: str | None,
    ) -> Page[s.ConnectionOut]:
        pdp = self.sc.pdp
        flt = pdp.visible_scopes("connection:read")
        if not (flt.tenant_wide or flt.workspace_ids or flt.project_ids):
            pdp.require("connection:read", self.sc.tenant_ref)  # 403 + decision record
        ws_id = pr_id = None
        if project and not workspace:
            raise ValidationFailed("`project` requires `workspace`")
        if include_shared and not workspace:
            raise ValidationFailed("`include_shared` requires `workspace`")
        if workspace:
            ref = tenancy.scope_ref_from_slugs(self.sc, workspace, project)
            ws_id, pr_id = ref.workspace_id, ref.project_id
        rows = repository.list_connections(
            self._session,
            tenant_id=self.sc.tenant_id,
            connection_type=connection_type,
            status=status,
            workspace_id=ws_id,
            project_id=pr_id,
            include_shared=include_shared,
            tenant_wide=flt.tenant_wide,
            visible_workspaces=sorted(flt.workspace_ids),
            visible_projects=sorted(flt.project_ids),
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        slugs = tenancy.scope_slug_map(
            self.sc,
            [r["workspace_id"] for r in page if r["workspace_id"]],
            [r["project_id"] for r in page if r["project_id"]],
        )
        return Page(
            items=[self._out(r, slugs) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, connection_id: UUID) -> s.ConnectionOut:
        row = self._get_row(connection_id)
        self._require("connection:read", row)
        return self._out(row)

    def create(self, body: s.ConnectionCreate) -> s.ConnectionOut:
        if body.credential is not None and body.secret_id is not None:
            raise ValidationFailed("send either credential or secret_id, not both")
        ref = tenancy.scope_ref_from_slugs(self.sc, body.scope.workspace, body.scope.project)
        self.sc.pdp.require("connection:manage", ref)
        if body.credential is not None:  # the connection's own secret is created (B-2748)
            self.sc.pdp.require("secret:manage", ref)
        self._active_tenant()
        self._active_scope(body.scope.workspace, body.scope.project)
        config = self._normalized(body.adapter_key, body.config)
        self._validate_definition(
            connection_type=body.connection_type,
            adapter_key=body.adapter_key,
            config=config,
            auth_method=body.auth_method,
            secret_id=body.secret_id,
            credential=body.credential is not None,
        )
        if body.credential is not None:
            value = body.credential.value.get_secret_value()
            secrets_service.check_value(value)  # the byte limit answers first
            self._check_credential(
                body.adapter_key, body.auth_method, value, ["body", "credential", "value"]
            )
        sec = self._check_secret(body.secret_id)
        self._check_secret_scope(sec, ref.workspace_id, ref.project_id)
        bound = body.secret_id is not None or body.credential is not None
        confirm = bound and body.access_mode == "read"
        if confirm:
            self._require_read_only_confirmation(body.read_only_confirmed)
        self._check_egress_policy(body.egress_policy_id)
        self._check_vendor(body.vendor_id)
        connection_id = uuid7()
        owned = None
        if body.credential is not None:
            owned = self._create_owned_secret(
                connection_id,
                body.auth_method,
                ref.workspace_id,
                ref.project_id,
                body.credential.value.get_secret_value(),
            )
        try:
            repository.insert_connection(
                self._session,
                {
                    "id": connection_id,
                    "tenant_id": self.sc.tenant_id,
                    "workspace_id": ref.workspace_id,
                    "project_id": ref.project_id,
                    "name": body.name,
                    "connection_type": body.connection_type,
                    "adapter_key": body.adapter_key,
                    "vendor_id": body.vendor_id,
                    "config": config,
                    "auth_method": body.auth_method,
                    "secret_ref_id": owned.id if owned is not None else body.secret_id,
                    "egress_policy_id": body.egress_policy_id,
                    "access_mode": body.access_mode,
                    "created_by": self.sc.principal.principal_id,
                },
            )
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            mapped = integrity_error(exc)
            if mapped is None:
                raise
            raise mapped from exc
        row = self._get_row(connection_id)
        self._audit(
            "connection.created",
            row,
            {
                "connection_type": row["connection_type"],
                "adapter": row["adapter_key"],
                "auth_method": row["auth_method"],
                "secret_set": row["secret_ref_id"] is not None,
                "access_mode": row["access_mode"],
                **({"read_only_confirmed": True} if confirm else {}),
            },
        )
        if owned is not None:
            self._audit_secret(
                "secret.created",
                owned,
                {"name": owned.name, "kind": owned.kind, "scope_type": owned.scope_type},
                row,
            )
        self._commit()
        return self._out(row)

    def update(self, connection_id: UUID, body: s.ConnectionPatch) -> s.ConnectionOut:
        row = self._get_row(connection_id)
        self._require("connection:manage", row)
        self._active_tenant()
        expected = self._checked_version(body.row_version, row)
        if row["status"] == "retired":
            raise Conflict("a retired connection cannot be modified")
        sent = body.model_fields_set - {"row_version", "read_only_confirmed"}
        if not sent:
            raise ValidationFailed("no updatable fields supplied")
        values: dict[str, Any] = {}
        if "name" in sent and body.name is not None:
            values["name"] = body.name
        if "config" in sent and body.config is not None:
            values["config"] = self._normalized(row["adapter_key"], body.config)
        if "auth_method" in sent and body.auth_method is not None:
            values["auth_method"] = body.auth_method
        if "secret_id" in sent:
            values["secret_ref_id"] = body.secret_id  # null detaches the secret
        if "egress_policy_id" in sent:
            values["egress_policy_id"] = body.egress_policy_id
        if "access_mode" in sent and body.access_mode is not None:
            values["access_mode"] = body.access_mode
        if "vendor_id" in sent:
            values["vendor_id"] = body.vendor_id
        merged_auth = values.get("auth_method", row["auth_method"])
        merged_secret = values.get("secret_ref_id", row["secret_ref_id"])
        desc = self._validate_definition(
            connection_type=row["connection_type"],
            adapter_key=row["adapter_key"],
            config=values.get("config", row["config"]),
            auth_method=merged_auth,
            secret_id=merged_secret,
        )
        if "secret_ref_id" in values and values["secret_ref_id"] != row["secret_ref_id"]:
            sec = self._check_secret(values["secret_ref_id"])
            self._check_secret_scope(sec, row["workspace_id"], row["project_id"])
        if (
            merged_secret is not None
            and merged_secret == row["secret_ref_id"]
            and ("config" in values or "auth_method" in values)
            and self._destination_changed(
                desc,
                (row["auth_method"], row["config"] or {}),
                (merged_auth, values.get("config", row["config"]) or {}),
            )
        ):
            # the secret stays bound and its destination moves: re-confirm with secret:use
            self._require_secret_use_for_host_change(merged_secret)
        merged_mode = values.get("access_mode", row["access_mode"])
        reconfirm = (
            merged_secret is not None
            and merged_mode == "read"
            and (merged_secret != row["secret_ref_id"] or row["access_mode"] != "read")
        )
        if reconfirm:  # a new credential, or an existing one now relied on as read-only
            self._require_read_only_confirmation(body.read_only_confirmed)
        if "egress_policy_id" in values and values["egress_policy_id"] != row["egress_policy_id"]:
            self._check_egress_policy(values["egress_policy_id"])
        if "vendor_id" in values:
            self._check_vendor(values["vendor_id"], current=row["vendor_id"])
        if CONFIG_AFFECTING & values.keys():
            # A changed definition invalidates the last test: re-test before (re)activation.
            values["last_tested_at"] = None
            values["health_status"] = "unknown"
            if row["status"] in ("active", "degraded"):
                values["status"] = "draft"
        try:
            updated = repository.update_connection(
                self._session,
                connection_id,
                expected,
                values,
                tenant_id=self.sc.tenant_id,
            )
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            mapped = integrity_error(exc)
            if mapped is None:
                raise
            raise mapped from exc
        if updated is None:
            raise Conflict("row_version mismatch: the connection was modified; reload and retry")
        self._audit(
            "connection.updated",
            updated,
            {
                "changed": sorted(sent),
                "status": updated["status"],
                "secret_set": updated["secret_ref_id"] is not None,
                **({"read_only_confirmed": True} if reconfirm else {}),
            },
        )
        fresh = self._get_row(connection_id)  # RETURNING has no `last_test_outcome`
        self._commit()
        return self._out(fresh)

    def set_status(self, connection_id: UUID, body: s.ConnectionStatusChange) -> s.ConnectionOut:
        row = self._get_row(connection_id)
        self._require("connection:manage", row)
        self._active_tenant()
        expected = self._checked_version(body.row_version, row)
        current, target = row["status"], body.status
        if target == current:
            return self._out(row)
        if target not in TRANSITIONS[current]:
            raise Conflict(f"a {current} connection cannot become {target}")
        values: dict[str, Any] = {"status": target}
        if target == "active":
            test = repository.latest_test(self._session, connection_id, tenant_id=self.sc.tenant_id)
            tested_at = row["last_tested_at"]
            fresh = (
                test is not None
                and test["outcome"] == "pass"
                and tested_at is not None
                and utcnow() - test["finished_at"] <= TEST_VALIDITY
                and test["config_hash"] == definition_fingerprint(row)  # THIS definition
            )
            if not fresh:
                raise Conflict(
                    "activation requires a passing connection test within the last 24 hours"
                )
        if target in ("disabled", "retired"):
            values["last_tested_at"] = None  # re-enabling requires a fresh test
        updated = repository.update_connection(
            self._session,
            connection_id,
            expected,
            values,
            tenant_id=self.sc.tenant_id,
        )
        if updated is None:
            raise Conflict("row_version mismatch: the connection was modified; reload and retry")
        self._audit("connection.status_changed", updated, {"from": current, "to": target})
        if target == "retired" and updated["secret_ref_id"] is not None:
            self._disable_owned_secret(updated)
        fresh = self._get_row(connection_id)  # RETURNING has no `last_test_outcome`
        self._commit()
        return self._out(fresh)

    def _disable_owned_secret(self, row: Mapping[str, Any]) -> None:
        """Archive (B-2751): the retired connection's own active secret is disabled unless
        another connection that is not retired binds it; any other secret is left unchanged."""
        sec = secrets_service.get_secret(
            self._session, row["secret_ref_id"], tenant_id=self.sc.tenant_id
        )
        if sec is None or sec.name != f"connection-{row['id']}" or sec.status != "active":
            return
        if repository.count_live_bindings(
            self._session, sec.id, tenant_id=self.sc.tenant_id, exclude=row["id"]
        ):
            return
        disabled = secrets_service.disable_secret(
            self._session, sec, expected_version=sec.row_version
        )
        self._audit_secret("secret.disabled", disabled, {}, row)

    def set_credential(
        self, connection_id: UUID, body: s.ConnectionCredentialSet
    ) -> s.ConnectionOut:
        """`PUT .../credential` (B-2749): with no secret bound, create and bind the connection's
        own `connection-<id>`; with one bound, rotate it in place. Either way the health is
        unknown again; status, last test and binding are otherwise unchanged."""
        row = self._get_row(connection_id)
        self._require("connection:manage", row)
        ref = ResourceRef(self.sc.tenant_id, row["workspace_id"], row["project_id"])
        self.sc.pdp.require("secret:manage", ref)
        self._active_tenant()
        if row["status"] == "retired":
            raise Conflict("a retired connection cannot be modified")
        if row["auth_method"] == "none":
            raise ValidationFailed(NO_SECRET_FOR_NONE)
        value = body.value.get_secret_value()
        secrets_service.check_value(value)
        self._check_credential(row["adapter_key"], row["auth_method"], value, ["body", "value"])
        expected = self._checked_version(body.row_version, row)
        if row["access_mode"] == "read":
            self._require_read_only_confirmation(body.read_only_confirmed)
        values: dict[str, Any] = {"health_status": "unknown"}
        bound = row["secret_ref_id"]
        if bound is None:
            sec = self._create_owned_secret(
                connection_id, row["auth_method"], row["workspace_id"], row["project_id"], value
            )
            values["secret_ref_id"] = sec.id
        else:
            current = secrets_service.get_secret(self._session, bound, tenant_id=self.sc.tenant_id)
            if current is None:
                raise NotFound("secret not found")
            sec = secrets_service.rotate_secret(
                self._session,
                self.runtime.settings.keyring,
                current,
                value=value,
                expected_version=current.row_version,
            )
        try:
            updated = repository.update_connection(
                self._session, connection_id, expected, values, tenant_id=self.sc.tenant_id
            )
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            mapped = integrity_error(exc)
            if mapped is None:
                raise
            raise mapped from exc
        if updated is None:
            self._session.rollback()  # neither the secret nor the connection changes
            raise Conflict("row_version mismatch: the connection was modified; reload and retry")
        if bound is None:
            self._audit(
                "connection.credential_set",
                updated,
                {"secret_id": str(sec.id), "version": sec.secret_version},
            )
            self._audit_secret(
                "secret.created",
                sec,
                {"name": sec.name, "kind": sec.kind, "scope_type": sec.scope_type},
                updated,
            )
        else:
            shared = repository.count_live_bindings(
                self._session, sec.id, tenant_id=self.sc.tenant_id, exclude=connection_id
            )
            self._audit(
                "connection.credential_rotated",
                updated,
                {"secret_id": str(sec.id), "version": sec.secret_version, "shared_with": shared},
            )
            self._audit_secret("secret.rotated", sec, {"version": sec.secret_version}, updated)
        fresh = self._get_row(connection_id)  # RETURNING has no `last_test_outcome`
        self._commit()
        return self._out(fresh)

    # ================================================================== runtime plumbing
    def _credential(self, row: Mapping[str, Any], purpose: str) -> Credential | None:
        if row["secret_ref_id"] is None:
            return None
        provider = secrets_service.SecretProvider(
            self._session, self.runtime.settings.keyring, self.sc.ctx
        )
        try:
            value = provider.resolve_system(
                row["secret_ref_id"],
                f"connection:{row['id']}:{purpose}",
                tenant_id=row["tenant_id"],  # the connection's own client (B-802)
            )
        except NotFound as exc:
            raise AdapterFailure(
                ErrorClass.CONFIG, "the connection's secret is unavailable (missing or disabled)"
            ) from exc
        return Credential(value.reveal())

    def _exec_ctx(self, row: Mapping[str, Any], deadline: float | None = None) -> ExecContext:
        guard = egress.build_guard(
            self._session,
            self.runtime,
            row["tenant_id"],
            row["egress_policy_id"],
            adapter_key=row["adapter_key"],
        )
        if deadline is not None:
            guard.set_deadline(deadline)
        return ExecContext(
            tenant_id=row["tenant_id"],  # the organization (carry_forward, adapters review 1)
            principal_id=self.sc.principal.principal_id,
            egress=guard,
            request_id=self.sc.ctx.request_id,
        )

    def _config(self, row: Mapping[str, Any], purpose: str) -> ConnectionConfig:
        return ConnectionConfig(
            id=row["id"],
            name=row["name"],
            adapter_key=row["adapter_key"],
            config=dict(row["config"] or {}),
            auth_method=row["auth_method"],
            access_mode=row["access_mode"],
            credential=self._credential(row, purpose),
        )

    def _adapter(self, row: Mapping[str, Any]) -> Any:  # Any: adapter instance
        desc = self._descriptor(row["adapter_key"])
        if not desc.implemented:
            raise AdapterNotImplemented(
                f"adapter {desc.key!r} is declared but not implemented (planned)"
            )
        return self.runtime.registry.get(row["adapter_key"])

    # ================================================================== test / discover
    def _testable(self, connection_id: UUID) -> tuple[Mapping[str, Any], Any]:
        """The connection row and its adapter, for a caller allowed to test it (404 when the
        connection is unreadable, 403 otherwise, 422 adapter_not_implemented when planned)."""
        row = self._get_row(connection_id)
        self._require("connection:test", row)
        return row, self._adapter(row)

    def enqueue_test(self, connection_id: UUID) -> s.TestJobOut:
        """Queue the connection test on the worker (ARCH M2, B-2610): the API process never calls
        the adapter. An active test job of the connection is answered instead of a second one."""
        self._testable(connection_id)
        p = self.sc.principal
        payload = {
            "connection_id": str(connection_id),
            "test_id": str(uuid7()),  # the result row's id, allocated now (payload is immutable)
            "principal_id": str(p.principal_id),
            "principal_type": p.principal_type,
            "api_key_id": str(p.api_key_id) if p.api_key_id else None,
        }
        job = repository.enqueue_test_job(
            self._session,
            tenant_id=self.sc.tenant_id,
            kind=TEST_JOB_KIND,
            job_id=uuid7(),
            connection_id=connection_id,
            payload=payload,
        )
        self._session.commit()
        return self._job_out(job)

    def test_job(self, connection_id: UUID, job_id: UUID) -> s.TestJobOut:
        row = self._get_row(connection_id)
        self._require("connection:read", row)
        job = repository.get_test_job(
            self._session,
            job_id,
            tenant_id=self.sc.tenant_id,
            kind=TEST_JOB_KIND,
            connection_id=connection_id,
        )
        if job is None:
            raise NotFound("test job not found")
        return self._job_out(job)

    def _job_out(self, job: Mapping[str, Any]) -> s.TestJobOut:
        payload = job["payload"]
        found = repository.get_test(
            self._session, UUID(payload["test_id"]), tenant_id=self.sc.tenant_id
        )
        status: Literal["queued", "running", "done", "failed"]
        error: str | None = None
        if found is not None:
            status = "done"
        elif job["status"] == "queued":
            status = "queued"
        elif job["status"] == "leased":
            status = "running"
        else:  # dead, or closed without a result (the reaper's `stale lease`)
            status = "failed"
            code = job["last_error"] or ""
            error = code if ERROR_CODE.fullmatch(code) else "worker_lost"
        return s.TestJobOut(
            id=job["id"],
            connection_id=UUID(payload["connection_id"]),
            status=status,
            result=_test_out(found) if found is not None else None,
            error=error,
            created_at=job["created_at"],
        )

    def execute_test(self, connection_id: UUID, test_id: UUID) -> s.TestResultOut:
        """Run the adapter test and record it as `test_id` (the worker side of `enqueue_test`)."""
        row, adapter = self._testable(connection_id)
        started = utcnow()
        t0 = time.monotonic()
        checks: list[dict[str, str]] = []
        error_class: str | None = None
        message: str | None = None
        try:
            cfg = self._config(row, "test")
            outcome = adapter.test(self._exec_ctx(row), cfg)
            checks = [
                {"name": c.name, "status": c.status, "detail": scrub_text(c.detail)[:500]}
                for c in outcome.checks
            ]
            passed = outcome.passed
            error_class = outcome.error_class.value if outcome.error_class else None
            message = scrub_text(outcome.message or "")[:500] or None
        except AdapterFailure as exc:
            passed = False
            error_class = exc.error_class.value
            message = scrub_text(exc.message)[:500]
            checks = [{"name": "config", "status": "fail", "detail": message}]
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 - an adapter bug must not leak details
            passed = False
            error_class = ErrorClass.UPSTREAM.value
            message = f"unexpected adapter error ({type(exc).__name__})"
            checks = [{"name": "capability", "status": "fail", "detail": message}]
        finished = utcnow()
        duration_ms = int((time.monotonic() - t0) * 1000)
        repository.insert_test(
            self._session,
            {
                "id": test_id,
                "tenant_id": self.sc.tenant_id,
                "connection_id": connection_id,
                "started_at": started,
                "finished_at": finished,
                "duration_ms": duration_ms,
                "outcome": "pass" if passed else "fail",
                "checks": checks,
                "error_class": error_class,
                "redacted_message": message,
                "config_version": row["row_version"],
                "config_hash": definition_fingerprint(row),
                "tested_by": self.sc.principal.principal_id,
            },
        )
        status: str | None = None
        if passed and row["status"] == "degraded":
            status = "active"
        elif not passed and row["status"] == "active":
            status = "degraded"
        repository.record_test_outcome(
            self._session,
            connection_id,
            tenant_id=self.sc.tenant_id,
            at=finished,
            health_status="healthy" if passed else "unhealthy",
            status=status,
        )
        self._audit(
            "connection.tested",
            row,
            {"outcome": "pass" if passed else "fail", "error_class": error_class, "checks": checks},
            outcome="success" if passed else "failure",
        )
        self._commit()
        return s.TestResultOut(
            id=test_id,
            connection_id=connection_id,
            outcome="pass" if passed else "fail",
            checks=[s.CheckOut(**c) for c in checks],
            error_class=error_class,
            duration_ms=duration_ms,
            started_at=started,
            finished_at=finished,
            created_at=finished,
        )

    def list_tests(
        self, connection_id: UUID, limit: int, cursor: str | None
    ) -> Page[s.TestResultOut]:
        row = self._get_row(connection_id)
        self._require("connection:read", row)
        rows = repository.list_tests(
            self._session,
            connection_id,
            decode_cursor(cursor),
            limit + 1,
            tenant_id=self.sc.tenant_id,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[_test_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def discover(self, connection_id: UUID) -> s.DiscoveryOut:
        row = self._get_row(connection_id)
        self._require("connection:test", row)
        adapter = self._adapter(row)
        try:
            result = adapter.discover(self._exec_ctx(row), self._config(row, "discover"))
        except AdapterFailure as exc:
            self._audit(
                "connection.discovered",
                row,
                {"error_class": exc.error_class.value},
                outcome="failure",
            )
            self._session.commit()
            raise failure_to_error(exc) from exc
        payload = json.loads(json.dumps(scrub(result.payload), default=str))
        discovery_id = uuid7()
        now = utcnow()
        repository.insert_discovery(
            self._session,
            {
                "id": discovery_id,
                "tenant_id": self.sc.tenant_id,
                "connection_id": connection_id,
                "discovered_at": now,
                "kind": result.kind,
                "payload": payload,
                "payload_hash": hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode()
                ).hexdigest(),
                "discovered_by": self.sc.principal.principal_id,
            },
        )
        self._audit("connection.discovered", row, {"kind": result.kind})
        self._commit()
        return s.DiscoveryOut(id=discovery_id, kind=result.kind, discovered_at=now, payload=payload)

    def latest_discovery(self, connection_id: UUID) -> s.DiscoveryOut:
        row = self._get_row(connection_id)
        self._require("connection:read", row)
        found = repository.latest_discovery(
            self._session, connection_id, tenant_id=self.sc.tenant_id
        )
        if found is None:
            raise NotFound("no discovery has been run for this connection")
        return s.DiscoveryOut(
            id=found["id"],
            kind=found["kind"],
            discovered_at=found["discovered_at"],
            payload=found["payload"],
        )

    def discovery_of(self, connection_id: UUID) -> s.DiscoveryOut | None:
        """Latest discovery of a tenant's connection, or None; authorization is the caller's job
        (validators of other modules that already established the connection is visible)."""
        row = self._get_row(connection_id)
        found = repository.latest_discovery(self._session, row["id"], tenant_id=self.sc.tenant_id)
        if found is None:
            return None
        return s.DiscoveryOut(
            id=found["id"],
            kind=found["kind"],
            discovered_at=found["discovered_at"],
            payload=found["payload"],
        )

    # ================================================================== use
    @contextmanager
    def open_trusted(
        self,
        row: Mapping[str, Any],
        purpose: str,
        *,
        write: bool = False,
        deadline: float | None = None,
    ) -> Iterator[ConnectionSession]:
        """Status / adapter / egress / secret gates, without a PDP check (the caller authorized
        the use through its own permission, e.g. `model:invoke`)."""
        self._own(row)
        if row["connection_type"] == "model_api":
            raise UseModelsEndpoint(
                "model connections are used through POST /models/{id}/invoke "
                "(model:invoke, allowlists and limits), not as raw connections"
            )
        adapter = self._adapter(row)  # planned adapters answer 422 before any status check
        if row["status"] not in USABLE_STATUSES:
            raise ConnectionDisabled(
                f"connection is {row['status']}; only active connections can be used"
            )
        if write and row["access_mode"] != "read_write":
            raise Forbidden("the connection is read-only")
        try:
            session = adapter.open(self._exec_ctx(row, deadline), self._config(row, purpose))
        except AdapterFailure as exc:
            raise failure_to_error(exc) from exc
        self._session.commit()  # secret-access rows are written; never held across the call
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def open_for_use(
        self,
        connection_id: UUID,
        purpose: str,
        write: bool = False,
        deadline: float | None = None,
        *,
        at: tuple[UUID | None, UUID | None] | None = None,
    ) -> Iterator[ConnectionSession]:
        """Authorized, audited adapter session for server-side callers (agents, pipelines, tools).

        ``connection:use`` is evaluated on the connection resource (a resource-level binding grants
        exactly that connection); ``write`` additionally needs ``connection:write``. ``deadline``
        (seconds, from the moment the session opens) bounds every network step of the session.
        ``at`` is a run's (workspace id, project id): the row read here must lie at the client
        level or at that workspace and project, else it is the unknown-connection answer (a run
        uses the row its caller checked; runtime isolation review 6 round 2)."""
        row = self._get_row(connection_id)
        if at is not None and not (
            row["workspace_id"] in (None, at[0]) and row["project_id"] in (None, at[1])
        ):
            raise NotFound("connection not found")
        self._require("connection:use", row)
        if write:
            self._require("connection:write", row, hide=None)
        with self.open_trusted(row, purpose, write=write, deadline=deadline) as session:
            self._audit("connection.used", row, {"purpose": purpose[:200], "write": write})
            self._session.commit()
            yield session

    def _parse_request(self, row: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        """Validate the invoke body against the connection type's request model (422)."""
        model = s.INVOKE_MODELS.get(row["connection_type"])
        if model is None:
            raise UseModelsEndpoint("this connection type has no direct invoke")
        try:
            return model.model_validate(body).model_dump()
        except PydanticValidationError as exc:
            raise ValidationFailed(
                "invoke request is invalid",
                errors=[
                    {"loc": ["body", *map(str, e["loc"])], "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ],
            ) from exc

    def _audit_invoke(
        self,
        row: Mapping[str, Any],
        request: Mapping[str, Any],
        write: bool,
        *,
        error_class: str | None = None,
        truncated: bool | None = None,
    ) -> None:
        details: dict[str, object] = {**_redacted_request(request), "write": write}
        if error_class:
            details["error_class"] = error_class
        if truncated is not None:
            details["truncated"] = truncated
        self._audit(
            "connection.invoked", row, details, outcome="failure" if error_class else "success"
        )
        self._session.commit()

    def invoke(self, connection_id: UUID, body: dict[str, Any]) -> s.InvokeOut:
        row = self._get_row(connection_id)
        self._require("connection:use", row)
        request: dict[str, Any] = {}
        write = False
        started = time.monotonic()
        try:
            if row["connection_type"] == "model_api":
                raise UseModelsEndpoint(
                    "model connections are used through POST /models/{id}/invoke "
                    "(model:invoke, allowlists and limits), not as raw connections"
                )
            request = self._parse_request(row, body)
            write = bool(request.pop("write", False))
            if write:
                self._require("connection:write", row, hide=None)
            with self.open_trusted(
                row,
                "api.invoke",
                write=write,
                deadline=self.runtime.settings.connection_call_deadline_seconds,
            ) as session:
                result = session.invoke(request, write=write)
        except AdapterFailure as exc:
            self._audit_invoke(row, request, write, error_class=exc.error_class.value)
            raise failure_to_error(exc) from exc
        except AppError as exc:
            self._audit_invoke(row, request, write, error_class=type(exc).__name__)
            raise
        except Exception as exc:  # noqa: BLE001 - never a 500 with details; always audited
            self._audit_invoke(row, request, write, error_class="unexpected")
            raise UpstreamError(f"unexpected adapter error ({type(exc).__name__})") from exc
        duration_ms = int((time.monotonic() - started) * 1000)
        self._audit_invoke(row, request, write, truncated=result.truncated)
        return s.InvokeOut(
            result=json.loads(json.dumps(scrub(result.result), default=str)),
            truncated=result.truncated,
            duration_ms=duration_ms,
        )

    # ================================================================== cross-module use
    def get_row(self, connection_id: UUID) -> Mapping[str, Any]:
        """The tenant's connection row (NotFound otherwise); authorization is the caller's job."""
        return self._get_row(connection_id)

    def readable(self, connection_id: UUID) -> bool:
        """Whether `connection_id` is a connection of this client the caller may read (False for
        an unknown id and for another client's, alike)."""
        return self.placement(connection_id) is not None

    def placement(self, connection_id: UUID) -> ConnectionPlacement | None:
        """The workspace, project and adapter of a connection of this client the caller may read;
        None for an unknown, another client's and an unreadable id alike."""
        try:
            row = self._get_row(connection_id)
        except NotFound:
            return None
        if not self.sc.pdp.authorize("connection:read", self.ref_of(row)).allowed:
            return None
        return ConnectionPlacement(row["workspace_id"], row["project_id"], row["adapter_key"])

    def prepare_call(
        self,
        row: Mapping[str, Any],
        purpose: str,
        *,
        require_usable: bool = True,
        deadline: float | None = None,
    ) -> tuple[Any, ExecContext, ConnectionConfig]:
        """(adapter, execution context, resolved config) for callers that drive the adapter
        themselves (model governance). Gates: adapter implemented, status, egress, secret."""
        self._own(row)
        adapter = self._adapter(row)
        blocked = (
            ("draft", "degraded", "disabled", "retired")
            if require_usable
            else ("disabled", "retired")
        )
        if row["status"] in blocked:
            raise ConnectionDisabled(f"connection is {row['status']}; it cannot be used")
        try:
            cfg = self._config(row, purpose)
        except AdapterFailure as exc:
            raise failure_to_error(exc) from exc
        self._session.commit()  # secret-access rows are written; never held across the call
        return adapter, self._exec_ctx(row, deadline), cfg

    def require_implemented(self, row: Mapping[str, Any]) -> None:
        """`prepare_call`'s first gates only (own row, adapter implemented: 422
        adapter_not_implemented when planned), for a caller that queues the call instead."""
        self._own(row)
        self._adapter(row)


def _test_out(r: Mapping[str, Any]) -> s.TestResultOut:
    return s.TestResultOut(
        id=r["id"],
        connection_id=r["connection_id"],
        outcome=r["outcome"],
        checks=[s.CheckOut(**c) for c in r["checks"]],
        error_class=r["error_class"],
        duration_ms=r["duration_ms"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        created_at=r["finished_at"],
    )


@dataclass(frozen=True)
class _Requester:
    """Who queued a test job (`authz.execution.RunRef`): re-derived when the job runs."""

    tenant_id: UUID
    initiated_by_principal_id: UUID
    principal_type: str
    api_key_id: UUID | None


def run_test_job(
    database: Database, runtime: IntegrationRuntime, tenant_id: UUID, job_id: UUID
) -> str | None:
    """Execute the `connection.test` job `job_id` of `tenant_id` (called by `shifde-worker`).

    Jobs carry no permissions: the requester is re-derived from the database and `connection:test`
    is checked again. Returns None once the test is recorded (now or by an earlier run of the same
    job), else the error code of the refusal; nothing is recorded then."""
    request_id = f"connection-test-{job_id}"
    session = database.session()
    db.init_context(session, request_id)
    try:
        db.set_tenant(session, tenant_id)
        job = repository.get_test_job(session, job_id, tenant_id=tenant_id, kind=TEST_JOB_KIND)
        if job is None:
            return NotFound.code
        payload = job["payload"]
        test_id = UUID(payload["test_id"])
        if repository.get_test(session, test_id, tenant_id=tenant_id) is not None:
            return None
        try:
            principal = authz.execution_principal(
                session,
                _Requester(
                    tenant_id,
                    UUID(payload["principal_id"]),
                    payload["principal_type"],
                    UUID(payload["api_key_id"]) if payload["api_key_id"] else None,
                ),
            )
            header = tenancy.tenant_header(session, tenant_id)
            if header is None:
                raise authz.ExecutionDenied("the client is not available")
            scope = TenantScope(
                session=session,
                ctx=RequestContext(request_id=request_id, principal=principal, tenant_id=tenant_id),
                principal=principal,
                tenant_id=tenant_id,
                tenant_slug=header[0],
                tenant_name=header[1],
                tenant_status=header[2],
                pdp=authz.new_pdp(session, principal, tenant_id),
            )
            ConnectionService(scope, runtime).execute_test(UUID(payload["connection_id"]), test_id)
        except (AppError, IntegrityError) as exc:
            session.rollback()
            # a unique violation on the pre-allocated id: another run of this job recorded it
            # (and its audit event and health update); nothing more is written
            if repository.get_test(session, test_id, tenant_id=tenant_id) is not None:
                return None
            if isinstance(exc, IntegrityError):
                raise
            return exc.code
        return None
    finally:
        session.close()


def definition_fingerprint(row: Mapping[str, Any]) -> str:
    """Hash of everything a connection test depends on (config, auth, secret, egress, mode)."""
    keys = (
        "adapter_key",
        "config",
        "auth_method",
        "secret_ref_id",
        "egress_policy_id",
        "access_mode",
    )
    doc = {k: row[k] for k in keys}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


def _redacted_request(request: Mapping[str, Any]) -> dict[str, object]:
    """Audit summary of an invocation: shape only, never parameter values."""
    out: dict[str, object] = {}
    if "method" in request or "path" in request:
        out["method"] = str(request.get("method", "GET")).upper()
        out["path"] = str(request.get("path", ""))[:300]
        out["query_keys"] = sorted((request.get("query") or {}).keys())
    if "tool" in request:
        out["tool"] = str(request["tool"])[:200]
        out["argument_keys"] = sorted((request.get("arguments") or {}).keys())
    if "sql" in request:
        out["sql"] = scrub_text(str(request["sql"]))[:500]
        params = request.get("params")
        out["param_count"] = len(params) if params is not None and hasattr(params, "__len__") else 0
    return out
