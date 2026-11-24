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

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any  # Any: rows and JSON specs
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.assist import catalog, repository
from shifde.assist import schemas as s
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.build import schemas as build_schemas
from shifde.build.service import ArtifactService
from shifde.core.errors import AppError, Conflict, Forbidden, NotFound, PublishPending
from shifde.core.ids import uuid7
from shifde.models_admin.service import ModelService
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


class SpecialistUnknown(AppError):
    status, code, title = 422, "specialist_unknown", "Unprocessable Entity"


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
    def __init__(self, scope: TenantScope, runtime: IntegrationRuntime) -> None:
        self.sc = scope
        self.runtime = runtime
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
