"""Assist settings, specialist install and per-project enable/disable (SP7 design sections 4, 8
and 11, Call S7-B).

The organization's administrator (`assist:admin`) switches assist on, which pins the catalog
version of the code (`catalog.CATALOG_VERSION`), and off, which keeps the pin; a missing settings
row reads as the defaults. Switching off makes every project's specialists unavailable without
rewriting them (`SpecialistOut.available`); the handover of SP7-3 refuses them.

Specialists are ordinary project-level `build` artifacts, installed per project through
`ArtifactService` under the caller's scope, so `authz` and `runtime` isolate them per project
unchanged. Install is never automatic. It checks, before any write: the project is visible
(404), the caller holds `assist:install` and every build permission the steps need there (403
naming the missing keys), assist is enabled on the current catalog version (409), the model can
be used in the project (the appgen mapping of `ModelService.problems_at`) and no requested
specialist is disabled (409). Then, per key in catalog order: the `assist_specialists` row is
created `installing` (committed); each tool, the prompt and the agent is a step that reuses the
project's artifact of the same key, keeps its latest published version when that version's spec
equals the generated one, and otherwise saves the generated spec as the draft and publishes it;
`build` commits each call, so every step is recorded in `install_progress` (row locked) right
after. The row then gets its agent and `enabled`. A publish waiting for approval stops the install
with `publish_pending` and leaves the row `installing`; installing again resumes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any  # Any: rows and JSON specs
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.appgen.schemas import AppRequestCreate
from shifde.appgen.service import AppGenService
from shifde.assist import catalog, repository, routing
from shifde.assist import schemas as s
from shifde.assist.routing import SpecialistUnknown
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.build import schemas as build_schemas
from shifde.build.service import ArtifactService
from shifde.connections.service import ConnectionService
from shifde.core import db
from shifde.core.context import RequestContext
from shifde.core.db import Database
from shifde.core.errors import (
    AppError,
    Conflict,
    Forbidden,
    NotFound,
    PublishPending,
    ScopeArchived,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.delivery.deployments import DeploymentService
from shifde.delivery.plans import PlanService
from shifde.delivery.schemas import PlanCreate
from shifde.delivery.service import TargetService
from shifde.evaluation.schemas import EvaluationRequest
from shifde.evaluation.service import EvaluationService
from shifde.models_admin import schemas as model_schemas
from shifde.models_admin.service import ModelService
from shifde.packaging.service import SolutionService, scoped_release
from shifde.runtime.governance import GovernanceHooks, TokenBudgetHooks
from shifde.runtime.schemas import RunCreate
from shifde.runtime.service import RunService
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

READ = "assist:request:read"
INSTALL = "assist:install"
ADMIN = "assist:admin"
# every permission the install steps need on the project (design section 4): the pre-check
INSTALL_PERMISSIONS = (
    INSTALL,
    "prompt:create",
    "prompt:update",
    "prompt:publish",
    "tool:create",
    "tool:update",
    "tool:publish",
    "agent:create",
    "agent:update",
    "agent:publish",
    "agent:activate",
)
TOGGLE_PERMISSIONS = (INSTALL, "agent:activate")
DEFAULT_MAX_COST = Decimal("1.0000")
SPECIALIST_NOT_FOUND = "specialist not found"
PUBLISH_NOTE = "installed by assist"
SETTINGS_TARGET = "assist_settings"
SPECIALIST_TARGET = "assist_specialist"
WRITE = "assist:request:write"
ROUTE_JOB_KIND = "assist.route"  # `shifde.jobs.kind` of the model fallback and handover recovery
REQUEST_TARGET = "assist_request"
REQUEST_NOT_FOUND = "assist request not found"
CONTEXT_NOT_FOUND = "context not found"
DONE = frozenset({"routed", "refused", "failed"})
ROUTER_MAX_TOKENS = 300
INVALID_OUTPUT = "invalid_model_output"
INSTALL_AGAIN = "the specialist differs from its catalog entry; run install again"
ACCEPT = "assist:proposal:accept"
PROPOSAL_TARGET = "assist_proposal"
PROPOSAL_NOT_FOUND = "proposal not found"
# the read permissions, on the request's project, of what a proposal's payload names: only a
# caller holding all of them, who can also read the connection or target the payload names, sees
# the payload (advisory item 5 of issue #280; issue #351)
PAYLOAD_READ: dict[str, tuple[str, ...]] = {
    "appgen_request": ("connection:read",),
    "evaluation_run": ("packaging:solution:read", "evaluation:run:read"),
    "delivery_plan": ("packaging:solution:read", "delivery:target:read", "delivery:plan:read"),
}
ROUTER_PROMPT = (
    "You route a person's request to exactly one SETH specialist. The user message is a JSON "
    'object with the request "text" and the "candidates" the routing rules found (empty when '
    "they found none). Answer with one JSON object and nothing else: "
    '{"specialist": <a specialist key, or null>, "reason": <at most 300 characters>}. When '
    '"candidates" is not empty pick one of them; otherwise pick any key below. Answer null when '
    "no specialist fits.\n\nSpecialists:\n"
    + "\n".join(f"- {e.key}: {e.router_description}" for e in catalog.CATALOG.values())
)
ROUTER_REPLY = Draft202012Validator(
    {
        "type": "object",
        "required": ["specialist", "reason"],
        "additionalProperties": False,
        "properties": {
            "specialist": {"type": ["string", "null"]},
            "reason": {"type": "string", "maxLength": 300},
        },
    }
)


class AssistNotEnabled(AppError):
    status, code, title = 409, "assist_not_enabled", "Conflict"


class SpecialistDisabled(AppError):
    status, code, title = 409, "specialist_disabled", "Conflict"


class SpecialistInstalling(AppError):
    status, code, title = 409, "specialist_installing", "Conflict"


class RouterModelRequired(AppError):
    status, code, title = 422, "router_model_required", "Unprocessable Entity"


class RouterModelNotAllowed(AppError):
    status, code, title = 422, "router_model_not_allowed", "Unprocessable Entity"


class ProposalNotOpen(AppError):
    status, code, title = 409, "proposal_not_open", "Conflict"


class ModelUnavailable(AppError):
    status, code, title = 422, "model_unavailable", "Unprocessable Entity"


class ModelNotAllowed(AppError):
    status, code, title = 422, "model_not_allowed", "Unprocessable Entity"


MODEL_REFUSALS: dict[str, AppError] = {
    "model_not_found": NotFound("model not found"),  # the answer an unknown model id gets
    "model_unavailable": ModelUnavailable("the specialists' model cannot be used"),
    "model_not_allowed": ModelNotAllowed("the specialists' model is not allowed here"),
}


def _comparable(kind: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    """`spec` as `build` stores it, for comparing a stored spec with a generated one: an agent's
    approval classes always include `destructive` (`build/agents.py`)."""
    out = dict(spec)
    if kind == "agents":
        rules = out.get("approval_rules") or {}
        classes = set(rules.get("require_approval_for") or []) | {"destructive"}
        out["approval_rules"] = {"require_approval_for": sorted(classes)}
    return out


@dataclass(frozen=True)
class _Refusal:
    """A handover pre-check that failed (design 5.4 steps 1-3)."""

    code: str
    missing: list[str] | None = None

    def details(self) -> dict[str, object]:
        if self.missing is not None:
            return {"missing_permissions": self.missing}
        return {"detail": INSTALL_AGAIN} if self.code == "specialist_modified" else {}


def _request_ref(tenant_id: UUID, row: Mapping[str, Any]) -> ResourceRef:
    return ResourceRef(tenant_id, row["workspace_id"], row["project_id"], REQUEST_TARGET, row["id"])


def _record_request(
    session: Session,
    ctx: RequestContext,
    tenant_id: UUID,
    action: str,
    row: Mapping[str, Any],
    extra: Mapping[str, object] | None = None,
) -> None:
    """The audit event of a request transition: the routing fields, never the text."""
    audit.record(
        session,
        ctx,
        action=action,
        tenant_id=tenant_id,
        target_type=REQUEST_TARGET,
        target_id=row["id"],
        workspace_id=row["workspace_id"],
        project_id=row["project_id"],
        details={
            "specialist_key": row["specialist_key"],
            "route_method": row["route_method"],
            "rule_id": row["rule_id"],
            "router_version": row["router_version"],
            "status_reason": row["status_reason"],
            **(extra or {}),
        },
    )


def _change(
    session: Session,
    ctx: RequestContext,
    tenant_id: UUID,
    row: Mapping[str, Any],
    expected: Sequence[str],
    status: str,
    extra: Mapping[str, object] | None = None,
    **fields: Any,
) -> Mapping[str, Any] | None:
    """Move the request to `status` while it is in `expected`, audit it and commit; None (rolled
    back) when it was in another state."""
    changed = repository.change_request(
        session, row["id"], tenant_id=tenant_id, expected=expected, status=status, **fields
    )
    if changed is None:
        session.rollback()
        return None
    _record_request(session, ctx, tenant_id, f"assist.request.{status}", changed, extra)
    session.commit()
    return changed


def _context_kind(row: Mapping[str, Any]) -> str | None:
    return str(row["context"]["kind"]) if row["context"] else None


def _candidates(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The routing candidates of a stored request (none once its text is scrubbed)."""
    if not row["text"]:
        return ()
    return routing.route(row["text"], row["requested_specialist"], _context_kind(row)).candidates


def _choice(reply: str, allowed: Sequence[str]) -> tuple[str | None, str | None]:
    """(key, None) for an allowed key the router model chose; (None, None) for null or a key
    outside `allowed`; (None, `invalid_model_output`) for a reply that is not the closed JSON
    object."""
    try:
        doc = json.loads(reply)
    except ValueError:
        return None, INVALID_OUTPUT
    if not ROUTER_REPLY.is_valid(doc):
        return None, INVALID_OUTPUT
    key = doc["specialist"]
    return (key, None) if key in allowed else (None, None)


def _canonical(spec: Mapping[str, Any] | None) -> str:
    return json.dumps(spec, sort_keys=True)


def _known(keys: Sequence[str]) -> None:
    unknown = [k for k in keys if k not in catalog.CATALOG]
    if unknown or len(set(keys)) != len(keys):
        raise SpecialistUnknown(
            "unknown or repeated specialist key",
            errors=[
                {
                    "loc": ["keys", k],
                    "msg": f"{k} is not a catalog key",
                    "type": "specialist_unknown",
                }
                for k in unknown
            ]
            or None,
        )


class AssistService:
    def __init__(
        self, scope: TenantScope, runtime: IntegrationRuntime, hooks: GovernanceHooks
    ) -> None:
        self.sc = scope
        self.runtime = runtime
        self.hooks = hooks
        self.artifacts = ArtifactService(scope, runtime)
        self.models = ModelService(scope, runtime)

    @property
    def _session(self) -> Session:
        return self.sc.session

    def _settings(self) -> dict[str, Any]:
        row = repository.get_settings(self._session, tenant_id=self.sc.tenant_id)
        if row is None:
            return {
                "assist_enabled": False,
                "catalog_version": None,
                "model_routing_enabled": False,
                "router_model_id": None,
                "specialist_max_cost": DEFAULT_MAX_COST,
                "updated_by": None,
                "updated_at": None,
            }
        return dict(row)

    @staticmethod
    def _live(settings: Mapping[str, Any]) -> bool:
        return bool(settings["assist_enabled"]) and (
            settings["catalog_version"] == catalog.CATALOG_VERSION
        )

    def _settings_out(self, settings: Mapping[str, Any]) -> s.AssistSettingsOut:
        return s.AssistSettingsOut(
            assist_enabled=settings["assist_enabled"],
            catalog_version=settings["catalog_version"],
            current_catalog_version=catalog.CATALOG_VERSION,
            model_routing_enabled=settings["model_routing_enabled"],
            router_model_id=settings["router_model_id"],
            specialist_max_cost=float(settings["specialist_max_cost"]),
            updated_at=settings["updated_at"],
        )

    def _specialist_out(self, row: Mapping[str, Any], live: bool) -> s.SpecialistOut:
        return s.SpecialistOut(
            id=row["id"],
            key=row["key"],
            title=catalog.CATALOG[row["key"]].title,
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            status=row["status"],
            agent_id=row["agent_id"],
            agent_version_id=row["agent_version_id"],
            catalog_version=row["catalog_version"],
            available=live and row["status"] == "enabled",
            installed_by=row["installed_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _project(self, ws: str, proj: str) -> ResourceRef:
        project = tenancy.scope_ref_from_slugs(self.sc, ws, proj)
        assert project.workspace_id is not None and project.project_id is not None
        return project

    def _require_all(self, actions: Sequence[str], ref: ResourceRef) -> None:
        """403 naming every one of `actions` the caller lacks at `ref`; the privileged ones are
        then checked through `require`, which records the decision."""
        missing = sorted(self.sc.pdp.missing_permissions(set(actions), ref))
        if missing:
            raise Forbidden(f"missing permission: {', '.join(missing)}")
        for action in actions:
            self.sc.pdp.require(action, ref)

    def _audit_settings(self, action: str, details: Mapping[str, object]) -> None:
        audit.record(
            self._session,
            self.sc.ctx,
            action=action,
            tenant_id=self.sc.tenant_id,
            target_type=SETTINGS_TARGET,
            target_id=self.sc.tenant_id,
            details=dict(details),
        )

    def _audit_specialist(
        self, action: str, row: Mapping[str, Any], details: Mapping[str, object]
    ) -> None:
        audit.record(
            self._session,
            self.sc.ctx,
            action=action,
            tenant_id=self.sc.tenant_id,
            target_type=SPECIALIST_TARGET,
            target_id=row["id"],
            target_label=f"{SPECIALIST_TARGET}:{row['key']}",
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            details=dict(details),
        )

    # ================================================================== settings
    def get_settings(self) -> s.AssistSettingsOut:
        self.sc.pdp.require(ADMIN, self.sc.tenant_ref)
        out = self._settings_out(self._settings())
        self._session.rollback()
        return out

    def put_settings(self, body: s.AssistSettingsPut) -> s.AssistSettingsOut:
        self.sc.pdp.require(ADMIN, self.sc.tenant_ref)
        if body.model_routing_enabled and body.router_model_id is None:
            raise RouterModelRequired("model routing needs a router model")
        if body.router_model_id is not None and self.models.problems_at(
            self.sc.tenant_ref, [body.router_model_id]
        ):
            raise RouterModelNotAllowed("the router model cannot be used by this organization")
        repository.put_routing(
            self._session,
            tenant_id=self.sc.tenant_id,
            model_routing_enabled=body.model_routing_enabled,
            router_model_id=body.router_model_id,
            specialist_max_cost=body.specialist_max_cost,
            updated_by=self.sc.principal.principal_id,
        )
        settings = self._settings()
        self._audit_settings(
            "assist.settings.updated",
            {
                "model_routing_enabled": body.model_routing_enabled,
                "router_model_id": str(body.router_model_id) if body.router_model_id else None,
                "specialist_max_cost": format(settings["specialist_max_cost"], "f"),
            },
        )
        self._session.commit()
        return self._settings_out(settings)

    def _switch(self, enabled: bool) -> s.AssistSettingsOut:
        self.sc.pdp.require(ADMIN, self.sc.tenant_ref)
        repository.put_switch(
            self._session,
            tenant_id=self.sc.tenant_id,
            enabled=enabled,
            pin=catalog.CATALOG_VERSION if enabled else None,
            updated_by=self.sc.principal.principal_id,
        )
        settings = self._settings()
        self._audit_settings(
            "assist.enabled" if enabled else "assist.disabled",
            {"catalog_version": settings["catalog_version"]},
        )
        self._session.commit()
        return self._settings_out(settings)

    def enable(self) -> s.AssistSettingsOut:
        return self._switch(True)

    def disable(self) -> s.AssistSettingsOut:
        return self._switch(False)

    # ================================================================== catalog
    def catalog(self) -> s.AssistCatalogOut:
        visible = self.sc.pdp.visible_scopes(READ)
        if not (visible.tenant_wide or visible.workspace_ids or visible.project_ids):
            raise Forbidden(f"missing permission: {READ}")
        live = self._live(self._settings())
        installs: dict[str, list[s.InstallStatus]] = {key: [] for key in catalog.KEYS}
        for row in repository.list_specialists(self._session, tenant_id=self.sc.tenant_id):
            if visible.covers_project(row["workspace_id"], row["project_id"]):
                installs[row["key"]].append(
                    s.InstallStatus(
                        workspace_id=row["workspace_id"],
                        project_id=row["project_id"],
                        status=row["status"],
                        available=live and row["status"] == "enabled",
                    )
                )
        self._session.rollback()
        return s.AssistCatalogOut(
            catalog_version=catalog.CATALOG_VERSION,
            assist_enabled=live,
            entries=[
                s.CatalogEntryOut(
                    key=e.key,
                    title=e.title,
                    router_description=e.router_description,
                    tools=list(e.tools),
                    required_permissions=list(e.required_permissions),
                    proposal_kinds=list(e.proposal_kinds),
                    installs=installs[e.key],
                )
                for e in catalog.CATALOG.values()
            ],
        )

    # ================================================================== specialists
    def _rows(self, project: ResourceRef, keys: Sequence[str] | None = None) -> list[Any]:
        assert project.project_id is not None
        rows = repository.list_specialists(
            self._session, tenant_id=self.sc.tenant_id, project_id=project.project_id
        )
        by_key = {r["key"]: r for r in rows}
        return [by_key[k] for k in catalog.KEYS if k in by_key and (keys is None or k in keys)]

    def list_specialists(self, ws: str, proj: str) -> list[s.SpecialistOut]:
        project = self._project(ws, proj)
        self.sc.pdp.require(READ, project)
        live = self._live(self._settings())
        out = [self._specialist_out(r, live) for r in self._rows(project)]
        self._session.rollback()
        return out

    def install(self, ws: str, proj: str, body: s.InstallIn) -> list[s.SpecialistOut]:
        keys = list(body.keys) if body.keys is not None else list(catalog.KEYS)
        _known(keys)
        keys = [k for k in catalog.KEYS if k in keys]
        project = self._project(ws, proj)
        self._require_all(INSTALL_PERMISSIONS, project)
        settings = self._settings()
        if not self._live(settings):
            raise AssistNotEnabled("assist is not enabled on the current catalog version")
        problem = self.models.problems_at(project, [body.model_id]).get(body.model_id)
        if problem is not None:
            raise MODEL_REFUSALS[problem]
        disabled = [r["key"] for r in self._rows(project, keys) if r["status"] == "disabled"]
        if disabled:
            raise SpecialistDisabled(
                f"enable the disabled specialists first: {', '.join(disabled)}"
            )
        self._session.rollback()
        max_cost = float(settings["specialist_max_cost"])
        for key in keys:
            self._install_one(project, key, body.model_id, max_cost)
        live = self._live(self._settings())
        out = [self._specialist_out(r, live) for r in self._rows(project, keys)]
        self._session.rollback()
        return out

    def _install_one(self, project: ResourceRef, key: str, model_id: UUID, max_cost: float) -> None:
        assert project.workspace_id is not None and project.project_id is not None
        tenant = self.sc.tenant_id
        row = repository.get_specialist(
            self._session, tenant_id=tenant, project_id=project.project_id, key=key
        )
        if row is None:
            repository.insert_specialist(
                self._session,
                {
                    "tenant_id": tenant,
                    "id": uuid7(),
                    "workspace_id": project.workspace_id,
                    "project_id": project.project_id,
                    "key": key,
                    "catalog_version": catalog.CATALOG_VERSION,
                    "installed_by": self.sc.principal.principal_id,
                },
            )
            self._session.commit()
        entry = catalog.CATALOG[key]
        tools = [
            self._step(project, key, "tools", catalog.tool_artifact_key(t), catalog.tool_spec(t))[1]
            for t in entry.tools
        ]
        name = catalog.artifact_key(key)
        prompt = self._step(project, key, "prompts", name, catalog.prompt_spec(key))[1]
        agent, version = self._step(
            project,
            key,
            "agents",
            name,
            catalog.agent_spec(key, model_id, prompt, tools, max_cost),
        )
        locked = repository.get_specialist(
            self._session, tenant_id=tenant, project_id=project.project_id, key=key, lock=True
        )
        assert locked is not None
        if not repository.finish_install(
            self._session,
            locked["id"],
            tenant_id=tenant,
            agent_id=agent,
            agent_version_id=version,
            catalog_version=catalog.CATALOG_VERSION,
        ):
            self._session.rollback()
            raise SpecialistDisabled("the specialist was disabled during the install")
        self._audit_specialist(
            "assist.specialist.installed",
            locked,
            {
                "catalog_version": catalog.CATALOG_VERSION,
                "agent_id": str(agent),
                "agent_version_id": str(version),
                "model_id": str(model_id),
            },
        )
        self._session.commit()

    def _step(
        self, project: ResourceRef, key: str, kind: str, artifact_key: str, spec: dict[str, Any]
    ) -> tuple[UUID, UUID]:
        """(artifact id, published version id) of the project's `artifact_key` artifact of `kind`
        with `spec`, created, saved and published as needed, then recorded in the progress."""
        holder = self.artifacts.key_holder(
            kind, project.workspace_id, project.project_id, artifact_key
        )
        if holder is None:
            artifact_id = self.artifacts.create(
                kind,
                build_schemas.ArtifactCreate(
                    key=artifact_key,
                    name=artifact_key,
                    scope=build_schemas.ScopeRef(type="project", id=project.project_id),
                    spec=spec,
                ),
            ).id
            version_id = self._publish(kind, artifact_id)
        else:
            artifact_id = holder
            art = self.artifacts.get(kind, artifact_id)
            latest = art.latest_published_version
            stored = (
                self.artifacts.get_version(kind, artifact_id, latest.id).spec
                if latest is not None
                else None
            )
            if (
                latest is not None
                and stored is not None
                and (_comparable(kind, stored) == _comparable(kind, spec))
            ):
                version_id = latest.id
            else:
                row_version = (
                    self.artifacts.get_draft(kind, artifact_id).row_version if art.has_draft else 0
                )
                self.artifacts.put_draft(
                    kind, artifact_id, build_schemas.DraftPut(spec=spec, row_version=row_version)
                )
                version_id = self._publish(kind, artifact_id)
        self._note(project, key, kind, artifact_key, artifact_id, version_id)
        return artifact_id, version_id

    def _publish(self, kind: str, artifact_id: UUID) -> UUID:
        out = self.artifacts.publish(
            kind, artifact_id, build_schemas.PublishIn(change_note=PUBLISH_NOTE)
        )
        if isinstance(out, build_schemas.PublishPendingOut):
            raise PublishPending("the publish waits for approval; install again once approved")
        return out.id

    def _note(
        self,
        project: ResourceRef,
        key: str,
        kind: str,
        artifact_key: str,
        artifact_id: UUID,
        version_id: UUID,
    ) -> None:
        """Record one step in the row's `install_progress` (row locked) and commit: a step seen
        before is replaced in place when its version changed."""
        assert project.project_id is not None
        row = repository.get_specialist(
            self._session,
            tenant_id=self.sc.tenant_id,
            project_id=project.project_id,
            key=key,
            lock=True,
        )
        assert row is not None
        step = f"{kind[:-1]}:{artifact_key}"
        entry = {
            "step": step,
            "artifact_key": artifact_key,
            "artifact_id": str(artifact_id),
            "version_id": str(version_id),
        }
        progress = list(row["install_progress"])
        at = next((i for i, p in enumerate(progress) if p.get("step") == step), None)
        if at is None:
            progress.append(entry)
        elif progress[at] != entry:
            progress[at] = entry
        else:
            self._session.rollback()
            return
        repository.set_progress(
            self._session, row["id"], tenant_id=self.sc.tenant_id, progress=progress
        )
        self._session.commit()

    def _toggle(self, ws: str, proj: str, key: str, enable: bool) -> s.SpecialistOut:
        _known([key])
        project = self._project(ws, proj)
        assert project.project_id is not None
        self._require_all(TOGGLE_PERMISSIONS, project)
        row = repository.get_specialist(
            self._session, tenant_id=self.sc.tenant_id, project_id=project.project_id, key=key
        )
        if row is None:
            raise NotFound(SPECIALIST_NOT_FOUND)
        if row["status"] == "installing":
            raise SpecialistInstalling("the specialist is still being installed; install again")
        status = "enabled" if enable else "disabled"
        self.artifacts.update(
            "agents",
            row["agent_id"],
            build_schemas.ArtifactPatch(status="active" if enable else "disabled"),
        )
        if not repository.change_status(
            self._session,
            row["id"],
            tenant_id=self.sc.tenant_id,
            expected=["enabled", "disabled"],
            status=status,
        ):
            self._session.rollback()
            raise Conflict("the specialist changed meanwhile; reload and retry")
        self._audit_specialist(
            f"assist.specialist.{status}", row, {"agent_id": str(row["agent_id"])}
        )
        self._session.commit()
        fresh = repository.get_specialist(
            self._session, tenant_id=self.sc.tenant_id, project_id=project.project_id, key=key
        )
        assert fresh is not None
        out = self._specialist_out(fresh, self._live(self._settings()))
        self._session.rollback()
        return out

    def enable_specialist(self, ws: str, proj: str, key: str) -> s.SpecialistOut:
        return self._toggle(ws, proj, key, True)

    def disable_specialist(self, ws: str, proj: str, key: str) -> s.SpecialistOut:
        return self._toggle(ws, proj, key, False)

    # ================================================================== requests (design 5.1)
    def _requester_fields(self) -> dict[str, str | None]:
        """Who sends a request, as its job's payload carries it (re-derived when the job runs)."""
        p = self.sc.principal
        return {
            "principal_id": str(p.principal_id),
            "principal_type": p.principal_type,
            "api_key_id": str(p.api_key_id) if p.api_key_id else None,
        }

    def _enqueue(self, request_id: UUID, job_id: UUID) -> None:
        repository.enqueue_job(
            self._session,
            tenant_id=self.sc.tenant_id,
            kind=ROUTE_JOB_KIND,
            job_id=job_id,
            dedupe_key=f"{ROUTE_JOB_KIND}:{request_id}",
            payload={"request_id": str(request_id), **self._requester_fields()},
        )

    def _active_project(self, ws: str, proj: str) -> None:
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")
        if tenancy.get_workspace(self.sc, ws).status != "active":
            raise ScopeArchived("workspace is archived")
        if tenancy.get_project(self.sc, ws, proj).status != "active":
            raise ScopeArchived("project is archived")

    def _in_project(self, ws: str, proj: str, project: ResourceRef, ctx: s.ContextIn) -> bool:
        """Whether the context object, read through its owning service under the caller's scope,
        is in the request's project (a target, plan or deployment: or its workspace)."""
        if ctx.kind == "solution":
            SolutionService(self.sc, self.runtime).get(ws, proj, ctx.id)
            return True
        if ctx.kind == "release":
            return bool(scoped_release(self.sc, ctx.id)["project_id"] == project.project_id)
        if ctx.kind == "evaluation_run":
            run = EvaluationService(self.sc, self.runtime, self.hooks).get_run(ctx.id)
            release = scoped_release(self.sc, run.release_id)
            return bool(release["project_id"] == project.project_id)
        if ctx.kind == "plan":
            target_id = PlanService(self.sc, self.runtime).get(ctx.id).target_id
        elif ctx.kind == "deployment":
            target_id = DeploymentService(self.sc).get(ctx.id).target_id
        else:
            target_id = ctx.id
        target = TargetService(self.sc).get(target_id)
        return target.workspace == ws and target.project in (None, proj)

    def _context(
        self, ws: str, proj: str, project: ResourceRef, ctx: s.ContextIn | None
    ) -> dict[str, str] | None:
        if ctx is None:
            return None
        try:
            found = self._in_project(ws, proj, project, ctx)
        except (NotFound, Forbidden):
            found = False
        if not found:
            raise NotFound(CONTEXT_NOT_FOUND)
        return {"kind": ctx.kind, "id": str(ctx.id)}

    def _drifted(self, project: ResourceRef, key: str, spec: Mapping[str, Any]) -> bool:
        """Whether the pinned agent version is no longer the catalog's (design 5.4 step 3): its
        agent is not active or not the project's, or its tools are not exactly the entry's
        internal tools with the generated specs."""
        if spec["agent_id"] is None or spec["agent_version_id"] is None:
            return True
        try:
            agent = self.artifacts.artifact_row("agents", spec["agent_id"])
        except NotFound:
            return True
        if agent["status"] != "active" or agent["project_id"] != project.project_id:
            return True
        version = self.artifacts.version_access(spec["agent_version_id"])
        if version is None or version.artifact_id != agent["id"]:
            return True
        agent_spec = self.artifacts.version_spec(version.version_id) or {}
        tools: list[str] = []
        for raw in agent_spec.get("tool_version_ids") or []:
            access = self.artifacts.version_access(UUID(str(raw)))
            if access is None or access.kind != "tools" or access.project_id != project.project_id:
                return True
            tools.append(_canonical(self.artifacts.version_spec(access.version_id)))
        expected = [_canonical(catalog.tool_spec(t)) for t in catalog.CATALOG[key].tools]
        return sorted(tools) != sorted(expected)

    def _precheck(
        self, project: ResourceRef, key: str, settings: Mapping[str, Any]
    ) -> Mapping[str, Any] | _Refusal:
        """The project's specialist row for `key` when the handover may run (design 5.4 steps
        1-3), else the refusal."""
        assert project.project_id is not None
        if not self._live(settings):
            return _Refusal("specialist_disabled")
        spec = repository.get_specialist(
            self._session, tenant_id=self.sc.tenant_id, project_id=project.project_id, key=key
        )
        if spec is None:
            return _Refusal("specialists_not_installed")
        if spec["status"] != "enabled":
            return _Refusal("specialist_disabled")
        needed = set(catalog.CATALOG[key].required_permissions) | {"agent:execute"}
        missing = sorted(self.sc.pdp.missing_permissions(needed, project))
        if missing:
            return _Refusal("specialist_not_permitted", missing)
        if self._drifted(project, key, spec):
            return _Refusal("specialist_modified")
        return spec

    def _transition(
        self,
        row: Mapping[str, Any],
        expected: Sequence[str],
        status: str,
        extra: Mapping[str, object] | None = None,
        **fields: Any,
    ) -> Mapping[str, Any]:
        """`_change` under the caller's context; the request as it now is either way."""
        changed = _change(
            self._session, self.sc.ctx, self.sc.tenant_id, row, expected, status, extra, **fields
        )
        if changed is not None:
            return changed
        fresh = repository.get_request(
            self._session, tenant_id=self.sc.tenant_id, request_id=row["id"]
        )
        assert fresh is not None
        return fresh

    def _hand_over(
        self, row: Mapping[str, Any], spec: Mapping[str, Any], settings: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Design 5.4 steps 4 (after `handing_over` is committed) and 5: the run of the project's
        own specialist as the requester; a repeat returns the same run (idempotency key)."""
        ws_slugs, proj_slugs = tenancy.scope_slug_map(
            self.sc, [row["workspace_id"]], [row["project_id"]]
        )
        context = json.dumps(row["context"], sort_keys=True) if row["context"] else ""
        body = RunCreate(
            kind="agent",
            agent_id=spec["agent_id"],
            version_id=spec["agent_version_id"],
            workspace=ws_slugs[row["workspace_id"]],
            project=proj_slugs[row["project_id"]],
            input=row["text"],
            variables={"assist_request_id": str(row["id"]), "context": context},
        )
        runs = RunService(self.sc, self.runtime, self.hooks)
        try:
            created, _ = runs.create(
                body,
                f"assist:{row['id']}",
                max_cost=Decimal(settings["specialist_max_cost"]),
            )
            run = runs.get(created.id)  # by (tenant_id, run_id) under the requester's scope
        except AppError as exc:
            self._session.rollback()
            return self._transition(row, ["handing_over"], "failed", status_reason=exc.code)
        if run.project.id != row["project_id"]:
            return self._transition(
                row, ["handing_over"], "failed", status_reason="run_project_mismatch"
            )
        if run.status == "failed":
            return self._transition(
                row, ["handing_over"], "failed", status_reason=run.error_class, run_id=run.id
            )
        return self._transition(row, ["handing_over"], "routed", run_id=run.id)

    def create_request(self, ws: str, proj: str, body: s.AssistRequestIn) -> s.AssistRequestOut:
        """Store and route a request (design 5.1 to 5.4): rule or explicit routing hands over at
        once; an undecided request is queued for the model (`queued`, answered 202) or refused."""
        project = self._project(ws, proj)
        self.sc.pdp.require(WRITE, project)
        self._active_project(ws, proj)
        if body.specialist is not None and body.specialist not in catalog.CATALOG:
            raise SpecialistUnknown(f"{body.specialist} is not a catalog key")
        context = self._context(ws, proj, project, body.context)
        decision = routing.route(
            body.text, body.specialist, body.context.kind if body.context else None
        )
        settings = self._settings()
        request_id, job_id = uuid7(), uuid7()
        values: dict[str, Any] = {
            "tenant_id": self.sc.tenant_id,
            "id": request_id,
            "workspace_id": project.workspace_id,
            "project_id": project.project_id,
            "requested_by": self.sc.principal.principal_id,
            "text": body.text,
            "requested_specialist": body.specialist,
            "context": context,
            "status_reason": None,
            "specialist_key": decision.specialist,
            "route_method": decision.method if decision.specialist else None,
            "rule_id": decision.rule_id,
            "router_version": routing.ROUTER_VERSION,
            "job_id": None,
        }
        refusal: _Refusal | None = None
        spec: Mapping[str, Any] | None = None
        if decision.specialist is None:
            if settings["model_routing_enabled"]:
                values.update(status="queued", job_id=job_id)
            else:
                many = len(decision.candidates) >= 2
                refusal = _Refusal("route_ambiguous" if many else "route_unclear")
        else:
            checked = self._precheck(project, decision.specialist, settings)
            if isinstance(checked, _Refusal):
                refusal = checked
            else:
                spec = checked
                values.update(status="handing_over", job_id=job_id)
        if refusal is not None:
            values.update(status="refused", status_reason=refusal.code)
        repository.insert_request(self._session, values)
        if values["job_id"] is not None:
            # queued: the model fallback; handing_over: the recovery of an interrupted handover
            self._enqueue(request_id, job_id)
        row = repository.get_request(
            self._session, tenant_id=self.sc.tenant_id, request_id=request_id
        )
        assert row is not None
        ctx, tenant = self.sc.ctx, self.sc.tenant_id
        candidates = {"candidates": list(decision.candidates)}
        _record_request(self._session, ctx, tenant, "assist.request.created", row, candidates)
        if refusal is not None:
            extra = refusal.details()
            if refusal.code == "route_ambiguous":
                extra = {**extra, **candidates}
            _record_request(self._session, ctx, tenant, "assist.request.refused", row, extra)
        elif spec is not None:
            _record_request(self._session, ctx, tenant, "assist.request.handing_over", row)
        self._session.commit()
        if spec is not None:
            row = self._hand_over(row, spec, settings)
        out = self._out(row, refusal.missing if refusal is not None else None)
        self._session.rollback()
        return out

    def _out(self, row: Mapping[str, Any], missing: list[str] | None = None) -> s.AssistRequestOut:
        run: s.RunRefOut | None = None
        if row["run_id"] is not None:
            try:
                got = RunService(self.sc, self.runtime, self.hooks).get(row["run_id"])
                run = s.RunRefOut(id=got.id, status=got.status, error_code=got.error_class)
            except NotFound:
                run = None
        proposals = repository.list_proposals(
            self._session, tenant_id=self.sc.tenant_id, request_id=row["id"]
        )
        project = ResourceRef(self.sc.tenant_id, row["workspace_id"], row["project_id"])
        return s.AssistRequestOut(
            **_summary_fields(row),
            rule_id=row["rule_id"],
            router_version=row["router_version"],
            candidates=list(_candidates(row)),
            missing_permissions=missing,
            context=s.ContextIn(**row["context"]) if row["context"] else None,
            run=run,
            proposals=[self._proposal_out(p, project) for p in proposals],
        )

    def _proposal_out(self, p: Mapping[str, Any], project: ResourceRef) -> s.ProposalOut:
        """The proposal as the caller may see it: the payload only with the read permissions of
        what it names on the request's project and when the caller can read the connection or
        target it names, `result_ref` only when the caller can read the object it names (design
        section 6)."""
        ref = p["result_ref"]
        return s.ProposalOut(
            id=p["id"],
            run_id=p["run_id"],
            kind=p["kind"],
            payload=p["payload"] if self._payload_readable(p, project) else None,
            status=p["status"],
            status_reason=p["status_reason"],
            result_ref=ref if ref is not None and self._result_readable(ref) else None,
            decided_by=p["decided_by"],
            decided_at=p["decided_at"],
            created_at=p["created_at"],
            updated_at=p["updated_at"],
        )

    def _payload_readable(self, p: Mapping[str, Any], project: ResourceRef) -> bool:
        """Whether the caller holds every `PAYLOAD_READ` key on the request's project and can read
        the connection or target the payload names; an id that is missing, not a UUID, unknown,
        another client's or unreadable is False alike (no recorded decision, no commit)."""
        if self.sc.pdp.missing_permissions(set(PAYLOAD_READ[p["kind"]]), project):
            return False
        if p["kind"] == "evaluation_run":
            return True
        key = "connection_id" if p["kind"] == "appgen_request" else "target_id"
        try:
            named = UUID(str(p["payload"][key]))
        except (KeyError, TypeError, ValueError):
            return False
        if p["kind"] == "appgen_request":
            return ConnectionService(self.sc, self.runtime).readable(named)
        return TargetService(self.sc).readable(named)

    def _result_readable(self, ref: Mapping[str, Any]) -> bool:
        """Whether the caller can read the object an accepted proposal created (no settle step,
        no scoring: nothing commits)."""
        object_id = UUID(str(ref["id"]))
        evaluations = EvaluationService(self.sc, self.runtime, self.hooks)
        try:
            if ref["kind"] == "delivery_plan":
                return PlanService(self.sc, self.runtime).readable(object_id)
            if ref["kind"] == "evaluation_run":
                evaluations.stored_run(object_id)
            else:
                AppGenService(self.sc, self.runtime, evaluations).get(object_id)
        except (NotFound, Forbidden):
            return False
        return True

    def list_requests(
        self, ws: str, proj: str, *, status: str | None, limit: int, cursor: str | None
    ) -> Page[s.AssistRequestSummary]:
        project = self._project(ws, proj)
        self.sc.pdp.require(READ, project)
        assert project.project_id is not None
        rows = repository.list_requests(
            self._session,
            tenant_id=self.sc.tenant_id,
            project_id=project.project_id,
            status=status,
            before=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        self._session.rollback()
        return Page(
            items=[s.AssistRequestSummary(**_summary_fields(r)) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get_request(self, request_id: UUID) -> s.AssistRequestOut:
        """The request read from its row, under `assist:request:read` on its own project; an
        unknown id, another client's and an unreadable one get one answer."""
        row = repository.get_request(
            self._session, tenant_id=self.sc.tenant_id, request_id=request_id
        )
        if row is None:
            raise NotFound(REQUEST_NOT_FOUND)
        try:
            self.sc.pdp.require(READ, _request_ref(self.sc.tenant_id, row), hide_unless=READ)
        except NotFound as exc:
            raise NotFound(REQUEST_NOT_FOUND) from exc
        out = self._out(row)
        self._session.rollback()
        return out

    # ================================================================== proposals (design 6)
    def _proposal(self, proposal_id: UUID) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """(proposal, its request) under `assist:request:read` on the request's project; an
        unknown id, another client's and an unreadable one get one answer."""
        p = repository.get_proposal(
            self._session, tenant_id=self.sc.tenant_id, proposal_id=proposal_id
        )
        row = (
            repository.get_request(
                self._session, tenant_id=self.sc.tenant_id, request_id=p["request_id"]
            )
            if p is not None
            else None
        )
        if p is None or row is None:
            raise NotFound(PROPOSAL_NOT_FOUND)
        try:
            self.sc.pdp.require(READ, _request_ref(self.sc.tenant_id, row), hide_unless=READ)
        except NotFound as exc:
            raise NotFound(PROPOSAL_NOT_FOUND) from exc
        return p, row

    def _proposal_answer(self, p: Mapping[str, Any], row: Mapping[str, Any]) -> s.ProposalOut:
        out = self._proposal_out(p, ResourceRef(self.sc.tenant_id, row["workspace_id"],
                                                row["project_id"]))  # fmt: skip
        self._session.rollback()
        return out

    def _audit_proposal(self, action: str, p: Mapping[str, Any], row: Mapping[str, Any]) -> None:
        """The audit event of a proposal decision: kind, status reason and result, no payload."""
        audit.record(
            self._session,
            self.sc.ctx,
            action=action,
            tenant_id=self.sc.tenant_id,
            target_type=PROPOSAL_TARGET,
            target_id=p["id"],
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            details={
                "kind": p["kind"],
                "request_id": str(row["id"]),
                "status_reason": p["status_reason"],
                "result_ref": p["result_ref"],
            },
        )

    def accept_proposal(self, proposal_id: UUID) -> s.ProposalOut:
        """Accept a proposal as the caller, through the owning service, in the request's project
        (design section 6). An accepted proposal is answered unchanged; a dismissed or failed one
        is 409. The proposal is claimed in a committed conditional UPDATE before the service call,
        so of two concurrent accepts one creates the object and the other gets 409. An accept whose
        proposal was dismissed while it held the claim is 409 too; when its object was created
        meanwhile, `assist.proposal.accept_superseded`, committed before the 409, names it."""
        p, row = self._proposal(proposal_id)
        self.sc.pdp.require(ACCEPT, _request_ref(self.sc.tenant_id, row), hide_unless=READ)
        if p["status"] == "accepted":
            return self._proposal_answer(p, row)
        if p["status"] != "open":
            raise ProposalNotOpen("the proposal is no longer open")
        me = self.sc.principal.principal_id
        claimed = repository.claim_proposal(
            self._session, p["id"], tenant_id=self.sc.tenant_id, decided_by=me
        )
        if claimed is None:
            self._session.rollback()
            fresh = repository.get_proposal(
                self._session, tenant_id=self.sc.tenant_id, proposal_id=p["id"]
            )
            if fresh is not None and fresh["status"] == "accepted":
                return self._proposal_answer(fresh, row)
            raise ProposalNotOpen("the proposal is no longer open")
        self._session.commit()
        try:
            result_ref: dict[str, str] | None = self._accepted(claimed, row)
            status, reason = "accepted", None
        except AppError as exc:
            self._session.rollback()
            result_ref, status, reason = None, "failed", exc.code
        decided = repository.decide_proposal(
            self._session,
            p["id"],
            tenant_id=self.sc.tenant_id,
            expected=["open"],
            status=status,
            decided_by=me,
            status_reason=reason,
            result_ref=result_ref,
        )
        if decided is None:  # dismissed while this accept held the claim
            if result_ref is not None:
                self._audit_proposal(
                    "assist.proposal.accept_superseded", {**claimed, "result_ref": result_ref}, row
                )
                self._session.commit()
            raise ProposalNotOpen("the proposal is no longer open")
        self._audit_proposal(f"assist.proposal.{status}", decided, row)
        self._session.commit()
        return self._proposal_answer(decided, row)

    def _accepted(self, p: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, str]:
        """Create the proposed object through its owning service as the caller, in the request's
        project; its `result_ref`. Any refusal is the service's `AppError`."""
        ws_slugs, proj_slugs = tenancy.scope_slug_map(
            self.sc, [row["workspace_id"]], [row["project_id"]]
        )
        ws, proj = ws_slugs[row["workspace_id"]], proj_slugs[row["project_id"]]
        evaluations = EvaluationService(self.sc, self.runtime, self.hooks)
        try:
            if p["kind"] == "appgen_request":
                body = AppRequestCreate(**p["payload"])
                created = AppGenService(self.sc, self.runtime, evaluations).create(ws, proj, body)
                return {"kind": p["kind"], "id": str(created.id)}
            release_id = UUID(str(p["payload"]["release_id"]))
            if scoped_release(self.sc, release_id)["project_id"] != row["project_id"]:
                raise NotFound("release not found")
            if p["kind"] == "evaluation_run":
                run = evaluations.request(release_id, EvaluationRequest())
                return {"kind": p["kind"], "id": str(run.id)}
            plan = PlanCreate(**p["payload"])
            target = TargetService(self.sc).get(plan.target_id)
            if target.workspace != ws or target.project not in (None, proj):
                raise NotFound("target not found")
            return {
                "kind": p["kind"],
                "id": str(PlanService(self.sc, self.runtime).request(plan).id),
            }
        except (ValidationError, KeyError, ValueError) as exc:
            raise ValidationFailed("the proposal's payload is not valid") from exc

    def dismiss_proposal(self, proposal_id: UUID) -> s.ProposalOut:
        """Dismiss an open or failed proposal; a dismissed one is answered unchanged, an accepted
        one is 409."""
        p, row = self._proposal(proposal_id)
        self.sc.pdp.require(ACCEPT, _request_ref(self.sc.tenant_id, row), hide_unless=READ)
        if p["status"] == "dismissed":
            return self._proposal_answer(p, row)
        decided = repository.decide_proposal(
            self._session,
            p["id"],
            tenant_id=self.sc.tenant_id,
            expected=["open", "failed"],
            status="dismissed",
            decided_by=self.sc.principal.principal_id,
            status_reason=p["status_reason"],
        )
        if decided is None:
            self._session.rollback()
            fresh = repository.get_proposal(
                self._session, tenant_id=self.sc.tenant_id, proposal_id=p["id"]
            )
            if fresh is not None and fresh["status"] == "dismissed":
                return self._proposal_answer(fresh, row)
            raise ProposalNotOpen("an accepted proposal cannot be dismissed")
        self._audit_proposal("assist.proposal.dismissed", decided, row)
        self._session.commit()
        return self._proposal_answer(decided, row)

    # ================================================================== the job (design 5.3)
    def _resume(self, row: Mapping[str, Any]) -> None:
        """An interrupted handover: the step 1-3 pre-checks again, then step 4 (the idempotency
        key returns the same run). A refusal fails the request with its code and starts no run
        (design 5.5 has no `handing_over -> refused`)."""
        settings = self._settings()
        project = ResourceRef(self.sc.tenant_id, row["workspace_id"], row["project_id"])
        checked = self._precheck(project, row["specialist_key"], settings)
        if isinstance(checked, _Refusal):
            self._transition(
                row, ["handing_over"], "failed", checked.details(), status_reason=checked.code
            )
            return
        self._hand_over(row, checked, settings)

    def route(self, row: Mapping[str, Any], database: Database) -> None:
        """The `assist.route` job's work for a `queued` or `handing_over` request, as the
        requester: one router model call, then the handover of design 5.4."""
        if row["status"] == "handing_over":
            self._resume(row)
            return
        settings = self._settings()
        if settings["router_model_id"] is None:
            self._transition(row, ["queued"], "failed", status_reason="router_model_missing")
            return
        candidates = _candidates(row)
        classifying = _change(
            self._session, self.sc.ctx, self.sc.tenant_id, row, ["queued"], "classifying"
        )
        if classifying is None:
            return
        # committed before the call: a job that finds it again never calls twice (design 5.3)
        body = model_schemas.InvokeIn(
            messages=[
                model_schemas.ChatMessageIn(role="system", content=ROUTER_PROMPT),
                model_schemas.ChatMessageIn(
                    role="user",
                    content=json.dumps({"text": row["text"], "candidates": list(candidates)}),
                ),
            ],
            max_tokens=ROUTER_MAX_TOKENS,
        )
        try:
            out = ModelService(self.sc, self.runtime, database).invoke_at(
                _request_ref(self.sc.tenant_id, row),
                settings["router_model_id"],
                body,
                usage_event_type="model_call",
            )
        except AppError as exc:
            self._session.rollback()
            self._transition(classifying, ["classifying"], "failed", status_reason=exc.code)
            return
        key, problem = _choice(out.output, candidates or catalog.KEYS)
        if key is None:
            self._transition(
                classifying,
                ["classifying"],
                "refused",
                {"detail": problem} if problem else None,
                status_reason="route_unclear",
            )
            return
        project = ResourceRef(self.sc.tenant_id, row["workspace_id"], row["project_id"])
        chosen: dict[str, Any] = {"specialist_key": key, "route_method": "model", "rule_id": None}
        checked = self._precheck(project, key, settings)
        if isinstance(checked, _Refusal):
            self._transition(
                classifying,
                ["classifying"],
                "refused",
                checked.details(),
                status_reason=checked.code,
                **chosen,
            )
            return
        handing = _change(
            self._session,
            self.sc.ctx,
            self.sc.tenant_id,
            classifying,
            ["classifying"],
            "handing_over",
            **chosen,
        )
        if handing is not None:
            self._hand_over(handing, checked, settings)


def record_proposal(
    scope: TenantScope,
    request: Mapping[str, Any],
    *,
    run_id: UUID,
    step_key: str,
    kind: str,
    payload: Mapping[str, Any],
) -> UUID:
    """Store a specialist's `open` proposal of the run step `step_key` with its
    `assist.proposal.created` audit event (kind only, never the payload), in the caller's
    transaction (a run step's: no commit). A replay of the step returns the stored proposal's id
    and writes nothing (UNIQUE (`tenant_id`, `run_id`, `step_key`))."""
    session, tenant = scope.session, scope.tenant_id
    proposal_id = repository.insert_proposal(
        session,
        {"tenant_id": tenant, "id": uuid7(), "request_id": request["id"], "run_id": run_id,
         "step_key": step_key, "kind": kind, "payload": dict(payload)},
    )  # fmt: skip
    if proposal_id is None:
        found = repository.step_proposal(
            session, tenant_id=tenant, run_id=run_id, step_key=step_key
        )
        assert found is not None
        return UUID(str(found["id"]))
    audit.record(
        session,
        scope.ctx,
        action="assist.proposal.created",
        tenant_id=tenant,
        target_type=PROPOSAL_TARGET,
        target_id=proposal_id,
        workspace_id=request["workspace_id"],
        project_id=request["project_id"],
        details={"kind": kind, "request_id": str(request["id"]), "run_id": str(run_id)},
    )
    return proposal_id


def _summary_fields(r: Mapping[str, Any]) -> dict[str, Any]:  # Any: field values
    return {
        "id": r["id"],
        "workspace_id": r["workspace_id"],
        "project_id": r["project_id"],
        "status": r["status"],
        "status_reason": r["status_reason"],
        "requested_specialist": r["requested_specialist"],
        "specialist_key": r["specialist_key"],
        "route_method": r["route_method"],
        "run_id": r["run_id"],
        "requested_by": r["requested_by"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


@dataclass(frozen=True)
class _Requester:
    """Who sent a request (`authz.execution.RunRef`): re-derived when its job runs."""

    tenant_id: UUID
    initiated_by_principal_id: UUID
    principal_type: str
    api_key_id: UUID | None


def _requester_scope(
    session: Session, tenant_id: UUID, request_id: str, payload: Mapping[str, Any]
) -> TenantScope:
    """The scope of the job's requester, re-derived from the database (`ExecutionDenied` when the
    requester or the client is no longer available)."""
    principal = authz.execution_principal(
        session,
        _Requester(
            tenant_id,
            UUID(payload["principal_id"]),
            payload["principal_type"],
            UUID(payload["api_key_id"]) if payload.get("api_key_id") else None,
        ),
    )
    header = tenancy.tenant_header(session, tenant_id)
    if header is None:
        raise authz.ExecutionDenied("the client is not available")
    return TenantScope(
        session=session,
        ctx=RequestContext(request_id=request_id, principal=principal, tenant_id=tenant_id),
        principal=principal,
        tenant_id=tenant_id,
        tenant_slug=header[0],
        tenant_name=header[1],
        tenant_status=header[2],
        pdp=authz.new_pdp(session, principal, tenant_id),
    )


def run_route_job(
    database: Database, runtime: IntegrationRuntime, tenant_id: UUID, job_id: UUID
) -> str | None:
    """Execute the `assist.route` job `job_id` of `tenant_id` (called by `shifde-worker`).

    Returns None once the outcome is recorded on the request (or there is nothing to do: the
    request is gone, already ended, or names another job), `not_found` when the job is not this
    client's route job.
    The router model is called at most once per request."""
    tag = f"assist-route-{job_id}"
    session = database.session()
    db.init_context(session, tag)
    try:
        db.set_tenant(session, tenant_id)
        job = repository.get_job(session, tenant_id=tenant_id, job_id=job_id, kind=ROUTE_JOB_KIND)
        if job is None:
            return NotFound.code
        payload = job["payload"]
        row = repository.get_request(
            session, tenant_id=tenant_id, request_id=UUID(payload["request_id"])
        )
        if row is None or row["status"] in DONE or row["job_id"] != job_id:
            return None
        system_ctx = RequestContext(request_id=tag, tenant_id=tenant_id)
        if row["status"] == "classifying":
            # an earlier attempt may have reached the provider: never call again
            _change(
                session,
                system_ctx,
                tenant_id,
                row,
                ["classifying"],
                "failed",
                status_reason="model_call_unknown",
            )
            return None
        try:
            scope = _requester_scope(session, tenant_id, tag, payload)
        except authz.ExecutionDenied:
            session.rollback()
            _change(
                session,
                system_ctx,
                tenant_id,
                row,
                [row["status"]],
                "failed",
                status_reason="access_revoked",
            )
            return None
        if not scope.pdp.authorize(WRITE, _request_ref(tenant_id, row)).allowed:
            _change(
                session,
                scope.ctx,
                tenant_id,
                row,
                [row["status"]],
                "failed",
                status_reason="access_revoked",
            )
            return None
        AssistService(scope, runtime, TokenBudgetHooks()).route(row, database)
        return None
    finally:
        session.close()
