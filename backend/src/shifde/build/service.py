"""Versioned artifacts (prompts, agents, tools, skills): lifecycle, drafts, publishing (DATA_MODEL
3.1, contract section 1).

* Visibility = (artifact visibility) x (the caller's `{kind}:read` grants). Anything the caller
  cannot see is a 404, including every other tenant's ids.
* Only a draft is mutable. Publishing validates the draft, resolves `latest:` references, pins
  dependencies by version id, computes `spec_hash` over the canonical resolved spec, links the pins
  (FKs), and moves `latest_published_version_id`. With `publish_requires_approval` the resolved
  draft is frozen and a publish request is created; a *different* principal holding
  `approval:decide` completes the publication.
* Every create / publish / state change / rollback / decision is audited.

Port (build part 4a, B-1928..): every `build.repository` call passes the scope's client id (the
row-level security policy is the second line, not the only one); ids are minted here, principal
columns come from the session and the client id from the `TenantScope`, never from a body, path
or header. The validators' `_Env` is built per request (`env`), answers what the caller may not
see like an unknown id, and skips the caller's checks only for an approver re-validating a
publish request already confirmed to be this client's (build isolation review 2 F1).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any  # Any: rows / specs
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.build import agents as agent_spec
from shifde.build import catalog, repository
from shifde.build import ontologies as ontology_spec
from shifde.build import orchestrations as orch_spec
from shifde.build import pipelines as pipeline_spec
from shifde.build import schemas as s
from shifde.build import skills as skill_spec
from shifde.build import tools as tool_spec
from shifde.build.canonical import diff as spec_diff
from shifde.build.canonical import spec_hash
from shifde.build.env import ConnectionInfo, McpDiscovery, VersionRef
from shifde.build.issues import Issue, Report
from shifde.build.prompts import validate_prompt
from shifde.connections.service import ConnectionService
from shifde.core.clock import utcnow
from shifde.core.errors import (
    AppError,
    Conflict,
    Forbidden,
    InvalidTransition,
    NotFound,
    PublishPending,
    ScopeArchived,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.identity import service as identity
from shifde.models_admin.service import ModelService
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

SINGULAR = {
    "prompts": "prompt",
    "agents": "agent",
    "tools": "tool",
    "skills": "skill",
    "orchestrations": "orchestration",
    "pipelines": "pipeline",
    "ontologies": "ontology",
}
MAX_SPEC_BYTES = 1_000_000
BATCH = 200
STATE_NEXT = {"published": "deprecated", "deprecated": "retired"}
APPROVAL_SETTING = "publish_requires_approval"


def issues_as_errors(issues: list[Issue]) -> list[dict[str, Any]]:
    """Issues in the problem+json `errors` shape (`loc`/`msg`/`type`) plus the contract names."""
    out: list[dict[str, Any]] = []
    for i in issues:
        parts = [p for p in i.path.replace("[", ".").replace("]", "").split(".") if p]
        entry: dict[str, Any] = {
            "loc": ["spec", *parts],
            "msg": i.message,
            "type": i.code,
            "path": i.path,
            "code": i.code,
            "message": i.message,
        }
        if i.node_id is not None:
            entry["node_id"] = i.node_id
        if i.key is not None:
            entry["key"] = i.key
        if i.edge is not None:
            entry["edge"] = {"from": i.edge[0], "to": i.edge[1]}
        out.append(entry)
    return out


class Prepared:
    """The outcome of validating a spec: the report, the spec to store, and the pins."""

    def __init__(
        self,
        report: Report,
        spec: dict[str, Any],
        pins: list[tuple[UUID, str]],
        refs: list[tuple[str, str, UUID]] | None = None,
        topo_order: list[str] | None = None,
    ) -> None:
        self.report = report
        self.spec = spec
        self.pins = pins
        self.refs = refs or []  # pipelines: (node id, ref type, id)
        self.topo_order = topo_order
        self.mappings_hash: str | None = None  # ontologies: hash of the mappings being frozen


class ArtifactService:
    def __init__(self, scope: TenantScope, runtime: IntegrationRuntime) -> None:
        self.sc = scope
        self.runtime = runtime
        self.conns = ConnectionService(scope, runtime)
        self.models = ModelService(scope, runtime)
        self._labels: dict[UUID, str] = {}
        self._maturity: dict[str, str] | None = None

    # ================================================================== helpers
    @property
    def session(self) -> Session:
        return self.sc.session

    def _active_tenant(self) -> None:
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")

    def _commit(self, what: str) -> None:
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise Conflict(f"{what} conflicts with an existing resource") from exc

    def _audit(
        self,
        action: str,
        art: Mapping[str, Any],
        details: dict[str, object],
        *,
        outcome: str = "success",
        target_type: str | None = None,
        target_id: UUID | None = None,
    ) -> None:
        audit.record(
            self.session,
            self.sc.ctx,
            action=action,
            outcome=outcome,
            tenant_id=self.sc.tenant_id,
            target_type=target_type or SINGULAR[art["kind"]],
            target_id=target_id or art["id"],
            target_label=f"{SINGULAR[art['kind']]}:{art['key']}",
            workspace_id=art["workspace_id"],
            project_id=art["project_id"],
            details=details,
        )

    def ref_of(self, art: Mapping[str, Any]) -> ResourceRef:
        return ResourceRef(
            self.sc.tenant_id,
            art["workspace_id"],
            art["project_id"],
            resource_type=SINGULAR[art["kind"]],
            resource_id=art["id"],
        )

    def scope_ref_of(self, art: Mapping[str, Any]) -> ResourceRef:
        """The artifact's owning scope (no resource id): where `model:invoke` etc. are checked."""
        return ResourceRef(self.sc.tenant_id, art["workspace_id"], art["project_id"])

    def visible(self, art: Mapping[str, Any]) -> bool:
        read = f"{SINGULAR[art['kind']]}:read"
        pdp = self.sc.pdp
        allowed = pdp.authorize(read, self.ref_of(art)).allowed
        if art["visibility"] == "private":
            return allowed and art["owner_principal_id"] == self.sc.principal.principal_id
        if allowed:
            return True
        if art["visibility"] == "tenant":
            held = pdp.visible_scopes(read)
            return held.tenant_wide or bool(held.workspace_ids or held.project_ids)
        return False

    def artifact(self, kind: str, artifact_id: UUID, perm: str | None = None) -> Mapping[str, Any]:
        """The caller-visible artifact of `kind` (else 404); with `perm`, also require
        `{kind}:{perm}` on it (403 - the artifact is visible so no existence is hidden)."""
        row = repository.get_artifact(self.session, self.sc.tenant_id, artifact_id)
        if (
            row is None
            or row["tenant_id"] != self.sc.tenant_id
            or row["kind"] != kind
            or not self.visible(row)
        ):
            raise NotFound(f"{SINGULAR[kind]} not found")
        if perm is not None:
            self.sc.pdp.require(f"{SINGULAR[kind]}:{perm}", self.ref_of(row))
        return row

    def artifact_row(self, kind: str, artifact_id: UUID) -> Mapping[str, Any]:
        """The tenant's artifact of `kind` WITHOUT the caller's visibility check: for execution
        paths whose caller was authorized at the Execution Gate (pipeline nodes, agent tools)."""
        row = repository.get_artifact(self.session, self.sc.tenant_id, artifact_id)
        if row is None or row["tenant_id"] != self.sc.tenant_id or row["kind"] != kind:
            raise NotFound(f"{SINGULAR[kind]} not found")
        return row

    def label(self, user_id: UUID | None) -> s.PrincipalRef | None:
        if user_id is None:
            return None
        if user_id not in self._labels:
            self._labels.update(identity.user_labels(self.session, [user_id]))
        return s.PrincipalRef(id=user_id, display=self._labels.get(user_id, str(user_id)))

    def _preload_labels(self, ids: set[UUID | None]) -> None:
        wanted = [i for i in ids if i is not None and i not in self._labels]
        if wanted:
            self._labels.update(identity.user_labels(self.session, wanted))

    def requires_approval(self) -> bool:
        return bool(tenancy.effective_settings(self.sc).get(APPROVAL_SETTING, False))

    def parallel_cap(self) -> int:
        configured = tenancy.effective_settings(self.sc).get("orchestration_max_parallel")
        cap = orch_spec.DEFAULT_PARALLEL_CAP
        if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
            cap = min(cap, configured)  # an operator can only tighten
        return cap

    def limit_maxima(self) -> dict[str, float]:
        maxima = dict(agent_spec.DEFAULT_MAXIMA)
        configured = tenancy.effective_settings(self.sc).get("agent_limit_maxima")
        if isinstance(configured, dict):
            for key in maxima:
                value = configured.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    maxima[key] = min(maxima[key], value)  # an operator can only tighten
        return maxima

    # ================================================================== outputs
    def _artifact_out(
        self, art: Mapping[str, Any], extras: Mapping[UUID, tuple[int | None, bool]]
    ) -> s.ArtifactOut:
        latest_no, has_draft = extras.get(art["id"], (None, False))
        if art["project_id"] is not None:
            scope = s.ScopeRef(type="project", id=art["project_id"])
        elif art["workspace_id"] is not None:
            scope = s.ScopeRef(type="workspace", id=art["workspace_id"])
        else:
            scope = s.ScopeRef(type="tenant", id=art["tenant_id"])
        owner = self.label(art["owner_principal_id"])
        assert owner is not None
        return s.ArtifactOut(
            id=art["id"],
            key=art["key"],
            name=art["name"],
            description=art["description"],
            scope=scope,
            visibility=art["visibility"],
            status=art["status"],
            latest_published_version=(
                s.VersionRefOut(id=art["latest_published_version_id"], version_no=latest_no)
                if art["latest_published_version_id"] is not None and latest_no is not None
                else None
            ),
            has_draft=has_draft,
            owner=owner,
            row_version=art["row_version"],
            created_at=art["created_at"],
            updated_at=art["updated_at"],
        )

    def artifact_out(self, art: Mapping[str, Any]) -> s.ArtifactOut:
        fresh = repository.get_artifact(self.session, self.sc.tenant_id, art["id"]) or art
        extras = repository.artifact_extras(self.session, self.sc.tenant_id, [art["id"]])
        return self._artifact_out(fresh, extras)

    def _pin_out(self, p: Mapping[str, Any]) -> s.PinOut:
        """A pin; the identity of an artifact the caller cannot see is redacted."""
        target = repository.get_artifact(self.session, self.sc.tenant_id, p["artifact_id"])
        if target is not None and self.visible(target):
            return s.PinOut(
                role=p["role"],
                version_id=p["version_id"],
                version_no=p["version_no"],
                state=p["state"],
                artifact_id=p["artifact_id"],
                kind=p["kind"],
                key=p["key"],
                name=p["name"],
            )
        return s.PinOut(
            role=p["role"],
            version_id=p["version_id"],
            version_no=0,
            state="hidden",
            artifact_id=p["artifact_id"],
            kind=p["kind"],
            key="",
            name="",
        )

    def _version_out(
        self, row: Mapping[str, Any], *, with_spec: bool, with_pins: bool = False
    ) -> s.VersionOut:
        self._preload_labels({row["created_by"], row["published_by"]})
        pins = None
        refs: list[dict[str, Any]] | None = None
        report: dict[str, Any] | None = None
        if with_pins:
            pins = [
                self._pin_out(p)
                for p in repository.list_pins(self.session, self.sc.tenant_id, row["id"])
            ]
            if row["kind"] == "pipelines" and row["state"] != "draft":
                refs = [
                    {
                        "node_id": r["node_id"],
                        "ref_type": r["ref_type"],
                        "ref_id": str(r["ref_id"]),
                    }
                    for r in repository.list_pipeline_refs(
                        self.session, self.sc.tenant_id, row["id"]
                    )
                ]
                stored = repository.get_validation_report(
                    self.session, self.sc.tenant_id, row["id"]
                )
                if stored is not None:
                    report = {**stored["report"], "topo_order": stored["topo_order"]}
        return s.VersionOut(
            id=row["id"],
            version_no=row["version_no"],
            state=row["state"],
            spec=row["spec"] if with_spec else None,
            spec_schema_version=row["spec_schema_version"],
            spec_hash=row["spec_hash"],
            change_note=row["change_note"],
            created_by=self.label(row["created_by"]),
            created_at=row["created_at"],
            published_by=self.label(row["published_by"]),
            published_at=row["published_at"],
            based_on_version_id=row["based_on_version_id"],
            row_version=row["row_version"],
            pins=pins,
            refs=refs,
            validation_report=report,
        )

    def _request_out(self, row: Mapping[str, Any], art: Mapping[str, Any]) -> s.PublishRequestOut:
        self._preload_labels({row["requested_by"], row["decided_by"]})
        requested_by = self.label(row["requested_by"])
        assert requested_by is not None
        return s.PublishRequestOut(
            id=row["id"],
            artifact=s.RequestArtifact(
                kind=row["kind"], id=art["id"], key=art["key"], name=art["name"]
            ),
            version_id=row["version_id"],
            version_no=row["version_no"],
            change_note=row["change_note"],
            requested_by=requested_by,
            requested_at=row["requested_at"],
            status=row["status"],
            decided_by=self.label(row["decided_by"]),
            decided_at=row["decided_at"],
            comment=row["comment"],
        )

    @staticmethod
    def _check_spec_size(spec: dict[str, Any]) -> None:
        try:
            size = len(json.dumps(spec, separators=(",", ":")))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationFailed("spec must be a JSON document") from exc
        if size > MAX_SPEC_BYTES:
            raise ValidationFailed(f"spec is larger than {MAX_SPEC_BYTES} bytes")

    # ================================================================== artifacts
    def list_artifacts(
        self,
        kind: str,
        *,
        scope_type: str | None,
        scope_id: UUID | None,
        status: str | None,
        q: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[s.ArtifactOut]:
        if q is not None and "\x00" in q:
            raise ValidationFailed("q: NUL characters are not allowed")
        if scope_type in ("workspace", "project") and scope_id is None:
            raise ValidationFailed("scope_id is required with scope_type")
        after = decode_cursor(cursor)
        found: list[Mapping[str, Any]] = []
        while len(found) <= limit:
            rows = repository.list_artifacts(
                self.session,
                self.sc.tenant_id,
                kind=kind,
                after=after,
                limit=BATCH,
                scope_type=scope_type,
                scope_id=scope_id,
                status=status,
                q=q,
            )
            for row in rows:
                after = row["id"]
                if self.visible(row):
                    found.append(row)
                    if len(found) > limit:
                        break
            if len(rows) < BATCH:
                break
        page, more = paginate(found, limit)
        self._preload_labels({r["owner_principal_id"] for r in page})
        extras = repository.artifact_extras(
            self.session, self.sc.tenant_id, [r["id"] for r in page]
        )
        return Page(
            items=[self._artifact_out(r, extras) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def create(self, kind: str, body: s.ArtifactCreate) -> s.ArtifactOut:
        self._active_tenant()
        single = SINGULAR[kind]
        ref = tenancy.resolve_scope_ref(self.sc, body.scope.type, body.scope.id)
        self.sc.pdp.require(f"{single}:create", ref, hide_unless=f"{single}:read")
        spec = body.spec or {}
        self._check_spec_size(spec)
        artifact_id, version_id = uuid7(), uuid7()
        try:
            repository.insert_artifact(
                self.session,
                {
                    "id": artifact_id,
                    "tenant_id": self.sc.tenant_id,
                    "kind": kind,
                    "workspace_id": ref.workspace_id,
                    "project_id": ref.project_id,
                    "key": body.key,
                    "name": body.name,
                    "description": body.description,
                    "owner_principal_id": self.sc.principal.principal_id,
                    "visibility": body.visibility,
                    "created_by": self.sc.principal.principal_id,
                },
            )
            self.session.flush()
            repository.insert_version(
                self.session,
                {
                    "id": version_id,
                    "tenant_id": self.sc.tenant_id,
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "version_no": 1,
                    "spec": spec,
                    "created_by": self.sc.principal.principal_id,
                    "based_on_version_id": None,
                },
            )
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise Conflict(f"a {single} with this key already exists at this scope") from exc
        row = repository.get_artifact(self.session, self.sc.tenant_id, artifact_id)
        assert row is not None
        self._audit(
            f"{single}.created",
            row,
            {"key": body.key, "visibility": body.visibility, "scope_type": body.scope.type},
        )
        self._commit(single)
        return self.artifact_out(row)

    def get(self, kind: str, artifact_id: UUID) -> s.ArtifactOut:
        return self.artifact_out(self.artifact(kind, artifact_id))

    def update(self, kind: str, artifact_id: UUID, body: s.ArtifactPatch) -> s.ArtifactOut:
        self._active_tenant()
        single = SINGULAR[kind]
        art = self.artifact(kind, artifact_id, "update")
        sent = body.model_fields_set - {"row_version"}
        if not sent:
            raise ValidationFailed("no updatable fields supplied")
        values: dict[str, Any] = {}
        for key in ("name", "visibility"):
            if key in sent and getattr(body, key) is not None:
                values[key] = getattr(body, key)
        if "description" in sent:
            values["description"] = body.description
        status_change = None
        if "status" in sent and body.status is not None and body.status != art["status"]:
            if art["status"] == "retired":
                raise InvalidTransition("a retired artifact is terminal")
            if kind == "agents":
                self.sc.pdp.require("agent:activate", self.ref_of(art))
            values["status"] = body.status
            status_change = (art["status"], body.status)
        if not values:
            return self.artifact_out(art)
        updated = repository.update_artifact(
            self.session, self.sc.tenant_id, artifact_id, body.row_version, values
        )
        if updated is None:
            raise Conflict("row_version mismatch: the artifact was modified; reload and retry")
        self._audit(f"{single}.updated", updated, {"changed": sorted(values)})
        if status_change:
            self._audit(
                f"{single}.status_changed",
                updated,
                {"from": status_change[0], "to": status_change[1]},
            )
        self._commit(single)
        return self.artifact_out(updated)

    # ================================================================== versions
    def version_of(self, art: Mapping[str, Any], version_id: UUID) -> Mapping[str, Any]:
        row = repository.get_version(self.session, self.sc.tenant_id, version_id)
        if row is None or row["artifact_id"] != art["id"]:
            raise NotFound("version not found")
        return row

    def list_versions(self, kind: str, artifact_id: UUID) -> list[s.VersionOut]:
        art = self.artifact(kind, artifact_id)
        rows = repository.list_versions(self.session, self.sc.tenant_id, art["id"])
        self._preload_labels({r["created_by"] for r in rows} | {r["published_by"] for r in rows})
        return [self._version_out(r, with_spec=False) for r in rows]

    def get_version(self, kind: str, artifact_id: UUID, version_id: UUID) -> s.VersionOut:
        art = self.artifact(kind, artifact_id)
        return self._version_out(self.version_of(art, version_id), with_spec=True, with_pins=True)

    def get_draft(self, kind: str, artifact_id: UUID) -> s.VersionOut:
        art = self.artifact(kind, artifact_id)
        draft = repository.get_draft(self.session, self.sc.tenant_id, art["id"])
        if draft is None:
            raise NotFound("this artifact has no draft")
        return self._version_out(draft, with_spec=True)

    def put_draft(self, kind: str, artifact_id: UUID, body: s.DraftPut) -> s.VersionOut:
        self._active_tenant()
        art = self.artifact(kind, artifact_id, "update")
        if art["status"] == "retired":
            raise InvalidTransition("a retired artifact cannot be edited")
        self._check_spec_size(body.spec)
        draft = repository.get_draft(self.session, self.sc.tenant_id, art["id"], lock=True)
        if draft is None:
            if body.row_version != 0:
                raise Conflict("row_version mismatch: there is no draft (use row_version 0)")
            try:
                version_id = uuid7()
                repository.insert_version(
                    self.session,
                    {
                        "id": version_id,
                        "tenant_id": self.sc.tenant_id,
                        "artifact_id": art["id"],
                        "kind": kind,
                        "version_no": repository.next_version_no(
                            self.session, self.sc.tenant_id, art["id"]
                        ),
                        "spec": body.spec,
                        "created_by": self.sc.principal.principal_id,
                        "based_on_version_id": art["latest_published_version_id"],
                    },
                )
                self.session.flush()
                if kind == "ontologies" and art["latest_published_version_id"] is not None:
                    # a new draft continues from the latest published version: so do its mappings
                    repository.copy_mappings(
                        self.session,
                        self.sc.tenant_id,
                        art["latest_published_version_id"],
                        version_id,
                        self.sc.principal.principal_id,
                    )
            except IntegrityError as exc:
                self.session.rollback()
                raise Conflict("a draft already exists; reload it") from exc
            saved = repository.get_version(self.session, self.sc.tenant_id, version_id)
        else:
            if (
                repository.pending_request_for(self.session, self.sc.tenant_id, draft["id"])
                is not None
            ):
                raise PublishPending("the draft is frozen while its publish request is pending")
            saved = repository.update_version(
                self.session, self.sc.tenant_id, draft["id"], body.row_version, {"spec": body.spec}
            )
            if saved is None:
                raise Conflict("row_version mismatch: the draft was modified; reload and retry")
        assert saved is not None
        self.session.commit()
        return self._version_out(saved, with_spec=True)

    # ================================================================== validation
    def env(
        self,
        art: Mapping[str, Any],
        *,
        approval_of: Mapping[str, Any] | None = None,
        run_of: Mapping[str, Any] | None = None,
    ) -> _Env:
        """The validators' `Env` for `art`, built for this request from this service's
        `TenantScope` (never cached). Every lookup carries the scope's client id.

        `approval_of` is the publish request an approver is re-validating: only then are the
        caller's visibility and use checks skipped (the requester was checked at request time),
        and only once the request, its artifact and `art` are confirmed to be this client's and
        to belong together; names stay hidden (`reveal_names=False`). Anything else is the
        unknown-id answer (B-1928).

        `run_of` is the run row the runtime executes (the Execution Gate authorizes each action of
        the run): the run is re-read with the scope's client id, `art` must be the artifact of the
        run's own pinned version (agent, orchestration, pipeline or ontology), and the env then
        answers only for that version and the versions it pins, directly or transitively, with
        no visibility or use check and names hidden (`latest` answers nothing: a run executes
        pinned versions). Another client's run, an unknown run or a run of another artifact is
        `NotFound("run not found")` (B-1933)."""
        tenant = self.sc.tenant_id
        if art["tenant_id"] != tenant:
            raise NotFound(f"{SINGULAR.get(art['kind'], 'artifact')} not found")
        if approval_of is not None and run_of is not None:
            raise ValueError("an env is for a publish request or for a run, not both")
        if run_of is not None:
            return self._run_env(art, run_of)
        if approval_of is None:
            return _Env(self, art, tenant, check_visibility=True)
        if (
            approval_of["tenant_id"] != tenant
            or approval_of["artifact_id"] != art["id"]
            or approval_of["kind"] != art["kind"]
        ):
            raise NotFound("publish request not found")
        confirmed = repository.get_request(self.session, tenant, approval_of["id"])
        if (
            confirmed is None
            or confirmed["tenant_id"] != tenant
            or confirmed["artifact_id"] != art["id"]
        ):
            raise NotFound("publish request not found")
        # the bypass belongs to the one who may decide a request that is still open (B-1931)
        self.sc.pdp.require("approval:decide", self.ref_of(art))
        if confirmed["requested_by"] == self.sc.principal.principal_id:
            raise Forbidden("separation of duties: you cannot decide your own publish request")
        if confirmed["status"] != "pending":
            raise Conflict(f"the publish request is already {confirmed['status']}")
        return _Env(self, art, tenant, check_visibility=False)

    def _run_env(self, art: Mapping[str, Any], run_of: Mapping[str, Any]) -> _Env:
        tenant = self.sc.tenant_id
        column = repository.RUN_TARGETS.get(art["kind"])
        if run_of["tenant_id"] != tenant or column is None:
            raise NotFound("run not found")
        run = repository.run_target(self.session, tenant, run_of["id"])
        target = run[column] if run is not None and run["tenant_id"] == tenant else None
        version = (
            repository.get_version(self.session, tenant, target) if target is not None else None
        )
        # the stored artifact row, never the caller's mapping (its workspace and project scope
        # the model checks; runtime isolation review 3 item 3)
        stored = repository.get_artifact(self.session, tenant, art["id"])
        if (
            version is None
            or stored is None
            or stored["tenant_id"] != tenant
            or stored["kind"] != art["kind"]
            or version["tenant_id"] != tenant
            or version["artifact_id"] != stored["id"]
            or version["kind"] != stored["kind"]
        ):
            raise NotFound("run not found")
        pinned = frozenset(repository.pin_closure(self.session, tenant, version["id"]))
        return _Env(
            self,
            stored,
            tenant,
            check_visibility=False,
            only_versions=pinned,
            run_version_id=version["id"],
        )

    def run_target(self, run_id: UUID) -> Mapping[str, Any] | None:
        """The target columns of a run of this client (`repository.run_target`), else None."""
        tenant = self.sc.tenant_id
        run = repository.run_target(self.session, tenant, run_id)
        return run if run is not None and run["tenant_id"] == tenant else None

    def run_versions(self, run: Mapping[str, Any]) -> list[Mapping[str, Any]]:  # Any: rows
        """The version rows a run executes: its own pinned version (agent, orchestration,
        pipeline or ontology) and every version that one pins, directly or transitively, all of
        this client (B-1933); empty for another client's run or an unknown one."""
        tenant = self.sc.tenant_id
        target = self.run_target(run["id"]) if run["tenant_id"] == tenant else None
        own = (
            next(
                (target[c] for c in repository.RUN_TARGETS.values() if target[c] is not None), None
            )
            if target is not None
            else None
        )
        if own is None:
            return []
        rows = (
            repository.get_version(self.session, tenant, vid)
            for vid in sorted(repository.pin_closure(self.session, tenant, own))
        )
        return [r for r in rows if r is not None and r["tenant_id"] == tenant]

    def prepare(
        self,
        art: Mapping[str, Any],
        spec: dict[str, Any],
        *,
        resolve: bool = True,
        approval_of: Mapping[str, Any] | None = None,
        run_of: Mapping[str, Any] | None = None,
    ) -> Prepared:
        env = self.env(art, approval_of=approval_of, run_of=run_of)
        if approval_of is not None:  # the spec validated for an approver is the frozen draft
            frozen = repository.get_request(self.session, self.sc.tenant_id, approval_of["id"])
            if frozen is None or spec_hash(spec) != frozen["spec_hash"]:
                raise Conflict("the draft changed after the request; ask for a new publish request")
        report = Report()
        kind = art["kind"]
        pins: list[tuple[UUID, str]] = []
        if kind == "prompts":
            stored = validate_prompt(spec, env, report)
            for item in stored.get("composed_of") or []:
                vid = _as_uuid(item.get("prompt_version_id")) if isinstance(item, dict) else None
                if vid is not None:
                    pins.append((vid, "composed_prompt"))
        elif kind == "skills":
            stored, deps = skill_spec.validate_skill(spec, env, report, art["id"])
            pins = [(vid, "skill_dependency") for vid in deps]
        elif kind == "tools":
            stored = tool_spec.validate_tool(spec, env, report, resolve=resolve)
        elif kind == "orchestrations":
            stored, orch_pins = orch_spec.validate_orchestration(
                spec, env, report, self.parallel_cap(), resolve=resolve
            )
            pins = [(ref.version_id, role) for ref, role in orch_pins]
        elif kind == "ontologies":
            # a run validates the mappings frozen with the version it executes, never the draft's
            # (runtime isolation review 3 item 4); a publish validates the draft's
            if env.run_version_id is not None:
                mapped: UUID | None = env.run_version_id
            else:
                draft_row = repository.get_draft(self.session, self.sc.tenant_id, art["id"])
                mapped = draft_row["id"] if draft_row else None
            mappings = (
                repository.list_mappings(self.session, self.sc.tenant_id, mapped)
                if mapped is not None
                else []
            )
            onto = ontology_spec.validate_ontology(
                spec, mappings, env, report, resolve=resolve, check_use=env.check_visibility
            )
            prepared_onto = Prepared(
                report, onto.spec, [(ref.version_id, role) for ref, role in onto.pins]
            )
            prepared_onto.mappings_hash = onto.mappings_hash
            return prepared_onto
        elif kind == "pipelines":
            piped = pipeline_spec.validate_pipeline(
                spec, env, report, self.parallel_cap(), resolve=resolve
            )
            return Prepared(
                report,
                piped.spec,
                [(ref.version_id, role) for ref, role in piped.pins],
                piped.refs,
                piped.topo_order,
            )
        else:
            stored, resolved = agent_spec.validate_agent(
                spec, env, report, art["id"], self.limit_maxima(), resolve=resolve
            )
            if resolved.prompt is not None:
                pins.append((resolved.prompt.version_id, "prompt"))
            pins += [(t.version_id, "tool") for t in resolved.tools]
            pins += [(k.version_id, "skill") for k in resolved.skills]
        return Prepared(report, stored, pins)

    def validate_draft(self, kind: str, artifact_id: UUID) -> s.ValidationOut:
        art = self.artifact(kind, artifact_id)
        draft = repository.get_draft(self.session, self.sc.tenant_id, art["id"])
        if draft is None:
            raise NotFound("this artifact has no draft")
        prepared = self.prepare(art, draft["spec"])
        report = prepared.report
        return s.ValidationOut(
            valid=report.valid,
            errors=[s.IssueOut.model_validate(i.as_dict()) for i in report.errors],
            warnings=[s.IssueOut.model_validate(i.as_dict()) for i in report.warnings],
            topo_order=prepared.topo_order,
        )

    # ================================================================== publishing
    def publish(
        self, kind: str, artifact_id: UUID, body: s.PublishIn
    ) -> s.VersionOut | s.PublishPendingOut:
        self._active_tenant()
        single = SINGULAR[kind]
        art = self._lock(self.artifact(kind, artifact_id, "publish"))
        if art["status"] != "active":
            raise Conflict(f"only an active {single} can be published (it is {art['status']})")
        # Row lock: a concurrent draft save / publish waits, then fails its row_version / state.
        draft = repository.get_draft(self.session, self.sc.tenant_id, art["id"], lock=True)
        if draft is None:
            raise NotFound("this artifact has no draft to publish")
        if repository.pending_request_for(self.session, self.sc.tenant_id, draft["id"]) is not None:
            raise PublishPending("a publish request for this draft is already pending")
        prepared = self.prepare(art, draft["spec"])
        if prepared.report.errors:
            self._audit(
                f"{single}.publish_rejected",
                art,
                {"version_no": draft["version_no"], "errors": len(prepared.report.errors)},
                outcome="denied",
                target_id=draft["id"],
                target_type=f"{single}_version",
            )
            self.session.commit()
            raise ValidationFailed(
                "the draft is invalid and cannot be published",
                errors=issues_as_errors(prepared.report.errors),
            )
        note = body.change_note.strip() or None
        # hash what jsonb will actually store (it normalises numbers)
        stored_spec = repository.normalize_json(self.session, prepared.spec)
        if self.requires_approval():
            frozen = repository.update_version(
                self.session,
                self.sc.tenant_id,
                draft["id"],
                draft["row_version"],
                {"spec": stored_spec, "change_note": note},
            )
            if frozen is None:
                raise Conflict("the draft was modified; reload and retry")
            request_id = uuid7()
            try:
                repository.insert_request(
                    self.session,
                    {
                        "id": request_id,
                        "tenant_id": self.sc.tenant_id,
                        "artifact_id": art["id"],
                        "kind": kind,
                        "version_id": draft["id"],
                        "version_no": draft["version_no"],
                        "change_note": note,
                        "spec_hash": spec_hash(frozen["spec"]),
                        "requested_by": self.sc.principal.principal_id,
                    },
                )
                self.session.flush()
            except IntegrityError as exc:
                self.session.rollback()
                raise PublishPending("a publish request for this draft is already pending") from exc
            self._audit(
                f"{single}.publish_requested",
                art,
                {"version_no": draft["version_no"], "request_id": str(request_id)},
                target_type="publish_request",
                target_id=request_id,
            )
            self.session.commit()
            req = repository.get_request(self.session, self.sc.tenant_id, request_id)
            assert req is not None
            return s.PublishPendingOut(publish_request=self._request_out(req, art))
        published = self._finalize(
            art,
            frozen_draft=draft,
            spec=stored_spec,
            pins=prepared.pins,
            note=note,
            published_by=self.sc.principal.principal_id,
            approved_by=None,
            approval_id=None,
            prepared=prepared,
        )
        return self._version_out(published, with_spec=True, with_pins=True)

    def _finalize(
        self,
        art: Mapping[str, Any],
        *,
        frozen_draft: Mapping[str, Any],
        spec: dict[str, Any],
        pins: list[tuple[UUID, str]],
        note: str | None,
        published_by: UUID,
        approved_by: UUID | None,
        approval_id: UUID | None,
        commit: bool = True,
        prepared: Prepared | None = None,
    ) -> Mapping[str, Any]:
        single = SINGULAR[art["kind"]]
        spec = repository.normalize_json(self.session, spec)
        digest = spec_hash(spec)
        try:
            updated = repository.update_version(
                self.session,
                self.sc.tenant_id,
                frozen_draft["id"],
                frozen_draft["row_version"],
                {
                    "spec": spec,
                    "spec_hash": digest,
                    "state": "published",
                    "change_note": note,
                    "published_by": published_by,
                    "published_at": utcnow(),
                    "approved_by": approved_by,
                    "approval_id": approval_id,
                },
            )
            if updated is None:
                self.session.rollback()
                raise Conflict("the draft was modified; reload and retry")
            repository.insert_pins(self.session, self.sc.tenant_id, updated["id"], pins)
            if art["kind"] == "pipelines" and prepared is not None:
                repository.insert_pipeline_refs(
                    self.session, self.sc.tenant_id, updated["id"], prepared.refs
                )
                repository.insert_validation_report(
                    self.session,
                    self.sc.tenant_id,
                    updated["id"],
                    {
                        "valid": True,
                        "errors": [],
                        "warnings": [i.as_dict() for i in prepared.report.warnings],
                    },
                    prepared.topo_order or [],
                )
            if art["kind"] == "ontologies" and prepared is not None:
                repository.insert_ontology_info(
                    self.session,
                    self.sc.tenant_id,
                    updated["id"],
                    prepared.mappings_hash or "",
                    {
                        "valid": True,
                        "errors": [],
                        "warnings": [i.as_dict() for i in prepared.report.warnings],
                    },
                )
            repository.refresh_latest_published(self.session, self.sc.tenant_id, art["id"])
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise Conflict("the draft could not be published; reload and retry") from exc
        self._audit(
            f"{single}.published",
            art,
            {
                "version_no": updated["version_no"],
                "spec_hash": digest,
                "pins": len(pins),
                "approved": approval_id is not None,
            },
            target_type=f"{single}_version",
            target_id=updated["id"],
        )
        if commit:
            self.session.commit()
        return updated

    # ================================================================== states / diff / rollback
    def set_state(
        self, kind: str, artifact_id: UUID, version_id: UUID, body: s.StateIn
    ) -> s.VersionOut:
        self._active_tenant()
        single = SINGULAR[kind]
        art = self.artifact(kind, artifact_id, "publish")
        art = self._lock(art)
        version = self.version_of(art, version_id)
        if STATE_NEXT.get(version["state"]) != body.state:
            raise InvalidTransition(
                f"a {version['state']} version cannot become {body.state} "
                "(published -> deprecated -> retired)"
            )
        updated = repository.set_state(self.session, self.sc.tenant_id, version_id, body.state)
        assert updated is not None
        repository.refresh_latest_published(self.session, self.sc.tenant_id, art["id"])
        self._audit(
            f"{single}.version_state_changed",
            art,
            {
                "version_no": version["version_no"],
                "from": version["state"],
                "to": body.state,
                "pinned_by": repository.pinned_by_count(
                    self.session, self.sc.tenant_id, version_id
                ),
            },
            target_type=f"{single}_version",
            target_id=version_id,
        )
        self._commit(single)
        return self._version_out(updated, with_spec=True, with_pins=True)

    def _spec_of(self, art: Mapping[str, Any], ref: str) -> dict[str, Any]:
        if ref == "draft":
            draft = repository.get_draft(self.session, self.sc.tenant_id, art["id"])
            if draft is None:
                raise NotFound("this artifact has no draft")
            return dict(draft["spec"])
        vid = _as_uuid(ref)
        if vid is None:
            raise ValidationFailed("a version id (or 'draft') is required")
        return dict(self.version_of(art, vid)["spec"])

    def diff(self, kind: str, artifact_id: UUID, from_: str, to: str) -> s.DiffOut:
        art = self.artifact(kind, artifact_id)
        changes = spec_diff(self._spec_of(art, from_), self._spec_of(art, to))
        out: list[dict[str, Any]] = []
        for c in changes:
            item: dict[str, Any] = {"path": c["path"], "op": c["op"]}
            if "old" in c:
                item["from"] = c["old"]
            if "new" in c:
                item["to"] = c["new"]
            out.append(item)
        return s.DiffOut(changes=out)

    def rollback(self, kind: str, artifact_id: UUID, body: s.RollbackIn) -> s.VersionOut:
        self._active_tenant()
        single = SINGULAR[kind]
        art = self._lock(self.artifact(kind, artifact_id, "update"))
        if art["status"] == "retired":
            raise InvalidTransition("a retired artifact cannot be edited")
        target = self.version_of(art, body.version_id)
        if target["state"] == "draft":
            raise ValidationFailed("roll back to a published, deprecated or retired version")
        spec = json.loads(json.dumps(target["spec"]))
        draft = repository.get_draft(self.session, self.sc.tenant_id, art["id"], lock=True)
        replaced = draft is not None
        if draft is None:
            version_id = uuid7()
            repository.insert_version(
                self.session,
                {
                    "id": version_id,
                    "tenant_id": self.sc.tenant_id,
                    "artifact_id": art["id"],
                    "kind": kind,
                    "version_no": repository.next_version_no(
                        self.session, self.sc.tenant_id, art["id"]
                    ),
                    "spec": spec,
                    "created_by": self.sc.principal.principal_id,
                    "based_on_version_id": target["id"],
                },
            )
            try:
                self.session.flush()
            except IntegrityError as exc:  # a concurrent draft creation won the one-draft index
                self.session.rollback()
                raise Conflict("a draft already exists; reload it") from exc
            saved = repository.get_version(self.session, self.sc.tenant_id, version_id)
        else:
            if (
                repository.pending_request_for(self.session, self.sc.tenant_id, draft["id"])
                is not None
            ):
                raise PublishPending("the draft is frozen while its publish request is pending")
            saved = repository.update_version(
                self.session,
                self.sc.tenant_id,
                draft["id"],
                None,
                {"spec": spec, "based_on_version_id": target["id"]},
            )
        assert saved is not None
        if kind == "ontologies":  # the draft's mappings are the rolled-back version's mappings
            repository.delete_mappings_of(self.session, self.sc.tenant_id, saved["id"])
            repository.copy_mappings(
                self.session,
                self.sc.tenant_id,
                target["id"],
                saved["id"],
                self.sc.principal.principal_id,
            )
        self._audit(
            f"{single}.rolled_back",
            art,
            {
                "to_version_no": target["version_no"],
                "draft_version_no": saved["version_no"],
                "replaced_draft": replaced,
            },
            target_type=f"{single}_version",
            target_id=saved["id"],
        )
        self._commit(single)
        return self._version_out(saved, with_spec=True)

    # ================================================================== publish requests
    def list_requests(
        self, status: str | None, limit: int, cursor: str | None
    ) -> Page[s.PublishRequestOut]:
        after = decode_cursor(cursor)
        found: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        while len(found) <= limit:
            rows = repository.list_requests(self.session, self.sc.tenant_id, status, after, BATCH)
            for row in rows:
                after = row["id"]
                art = repository.get_artifact(self.session, self.sc.tenant_id, row["artifact_id"])
                if art is not None and self._request_visible(art):
                    found.append((row, art))
                    if len(found) > limit:
                        break
            if len(rows) < BATCH:
                break
        page, more = paginate(found, limit)
        return Page(
            items=[self._request_out(r, a) for r, a in page],
            next_cursor=encode_cursor(page[-1][0]["id"]) if more and page else None,
        )

    def _lock(self, art: Mapping[str, Any]) -> Mapping[str, Any]:
        """Take the artifact row lock and return its fresh state (status checks happen on it)."""
        fresh = repository.lock_artifact(self.session, self.sc.tenant_id, art["id"])
        if fresh is None:
            raise NotFound("artifact not found")
        return fresh

    def _request_visible(self, art: Mapping[str, Any]) -> bool:
        """Publish requests are visible with the artifact, and to approval:decide holders at the
        artifact's scope even when the artifact itself is private (else a private artifact could
        never be approved: the owner is barred by separation of duties)."""
        return (
            self.visible(art) or self.sc.pdp.authorize("approval:decide", self.ref_of(art)).allowed
        )

    def decide(self, request_id: UUID, body: s.DecideIn) -> s.PublishRequestOut:
        self._active_tenant()
        req = repository.get_request(self.session, self.sc.tenant_id, request_id)
        art = (
            repository.get_artifact(self.session, self.sc.tenant_id, req["artifact_id"])
            if req is not None and req["tenant_id"] == self.sc.tenant_id
            else None
        )
        if (
            req is None
            or art is None
            or art["tenant_id"] != self.sc.tenant_id
            or not self._request_visible(art)
        ):
            raise NotFound("publish request not found")
        single = SINGULAR[art["kind"]]
        self.sc.pdp.require("approval:decide", self.ref_of(art))
        me = self.sc.principal.principal_id
        if req["requested_by"] == me:
            raise Forbidden("separation of duties: you cannot decide your own publish request")
        art = self._lock(art)  # lock order: artifact row first, then the version
        req = repository.get_request(self.session, self.sc.tenant_id, request_id) or req
        if req["status"] != "pending":
            raise Conflict(f"the publish request is already {req['status']}")
        comment = (body.comment or "").strip() or None
        now = utcnow()
        if body.decision == "reject":
            decided = repository.decide_request(
                self.session, self.sc.tenant_id, request_id, "rejected", me, now, comment
            )
            if decided is None:
                raise Conflict("the publish request is no longer pending")
            self._audit(
                f"{single}.publish_rejected_by_approver",
                art,
                {"request_id": str(request_id), "version_no": req["version_no"]},
                target_type="publish_request",
                target_id=request_id,
            )
            self._commit("publish request")
            return self._request_out(decided, art)

        if art["status"] != "active":
            raise Conflict(f"only an active {single} can be published (it is {art['status']})")
        draft = repository.get_version(
            self.session, self.sc.tenant_id, req["version_id"], lock=True
        )
        if draft is None or draft["state"] != "draft":
            raise Conflict("the requested draft is no longer open")
        if spec_hash(draft["spec"]) != req["spec_hash"]:
            raise Conflict("the draft changed after the request; ask for a new publish request")
        if draft["artifact_id"] != art["id"] or req["tenant_id"] != self.sc.tenant_id:
            raise NotFound("publish request not found")
        prepared = self.prepare(art, draft["spec"], resolve=False, approval_of=req)
        if prepared.report.errors:
            raise ValidationFailed(
                "the draft no longer validates and cannot be published",
                errors=issues_as_errors(prepared.report.errors),
            )
        decided = repository.decide_request(
            self.session, self.sc.tenant_id, request_id, "approved", me, now, comment
        )
        if decided is None:
            raise Conflict("the publish request is no longer pending")
        self._finalize(
            art,
            frozen_draft=draft,
            spec=draft["spec"],
            pins=prepared.pins,
            note=req["change_note"],
            published_by=req["requested_by"],
            approved_by=me,
            approval_id=request_id,
            commit=False,  # the approval audit row is written in the same transaction
            prepared=prepared,
        )
        self._audit(
            f"{single}.publish_approved",
            art,
            {"request_id": str(request_id), "version_no": req["version_no"]},
            target_type="publish_request",
            target_id=request_id,
        )
        self._commit("publish request")
        final = repository.get_request(self.session, self.sc.tenant_id, request_id)
        assert final is not None
        return self._request_out(final, art)

    # ================================================================== cross-module helpers
    def version_spec(self, version_id: UUID) -> dict[str, Any] | None:
        """The stored spec of a version of this tenant (no visibility check: for run read models
        whose caller was authorized on the run)."""
        row = repository.get_version(self.session, self.sc.tenant_id, version_id)
        if row is None or row["tenant_id"] != self.sc.tenant_id:
            return None
        return dict(row["spec"])

    def draft_of(self, art: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return repository.get_draft(self.session, self.sc.tenant_id, art["id"])

    def executor_maturities(self) -> dict[str, str]:
        if self._maturity is None:
            self._maturity = catalog.maturity_map(self.session, self.runtime.registry)
        return self._maturity


def _as_uuid(value: Any) -> UUID | None:  # Any: JSON value
    try:
        return UUID(str(value)) if isinstance(value, str) else None
    except ValueError:
        return None


class _Env:
    """The validators' view of the world, bound to the publisher's permissions and to one client.

    Built per request by `ArtifactService.env` from the service's `TenantScope`; the client id is
    fixed at construction and every lookup passes it to the repositories (row-level security is
    the second line, not the only one). Whatever the caller may not see answers exactly as an
    unknown id does: None, or `McpDiscovery(False, None)` (build isolation review 2 F1, B-1928)."""

    def __init__(
        self,
        svc: ArtifactService,
        art: Mapping[str, Any],
        tenant_id: UUID,
        *,
        check_visibility: bool,
        only_versions: frozenset[UUID] | None = None,
        run_version_id: UUID | None = None,
    ) -> None:
        self.svc = svc
        self.art = art
        self.tenant_id = tenant_id
        self.check_visibility = check_visibility
        self.reveal_names = check_visibility
        # a run's env (`run_of`): only the run's pinned version and what it pins
        self.only_versions = only_versions
        # a run's env: the run's own pinned version (`only_versions` holds it and its pins)
        self.run_version_id = run_version_id

    def _same_client(self) -> bool:
        """The scope still names the client this env was built for (an env is never reused)."""
        return self.svc.sc.tenant_id == self.tenant_id and self.art["tenant_id"] == self.tenant_id

    def _ref(self, art: Mapping[str, Any], version: Mapping[str, Any]) -> VersionRef | None:
        if (
            not self._same_client()
            or art["tenant_id"] != self.tenant_id
            or version["tenant_id"] != self.tenant_id
            or version["artifact_id"] != art["id"]
            or version["kind"] != art["kind"]
        ):
            return None
        # visibility at the artifact's own workspace and project (`visible` builds its
        # ResourceRef from the row); a private artifact only for its owner
        if self.check_visibility and not self.svc.visible(art):
            return None
        return VersionRef(
            version_id=version["id"],
            artifact_id=art["id"],
            kind=art["kind"],
            version_no=version["version_no"],
            state=version["state"],
            spec=dict(version["spec"]),
            spec_hash=version["spec_hash"],
            artifact_key=art["key"],
            artifact_name=art["name"],
            artifact_status=art["status"],
            workspace_id=art["workspace_id"],
            project_id=art["project_id"],
        )

    def version(self, version_id: UUID) -> VersionRef | None:
        if not self._same_client():
            return None
        if self.only_versions is not None and version_id not in self.only_versions:
            return None
        session = self.svc.session
        row = repository.get_version(session, self.tenant_id, version_id)
        if row is None:
            return None
        art = repository.get_artifact(session, self.tenant_id, row["artifact_id"])
        return self._ref(art, row) if art is not None else None

    def latest(self, artifact_id: UUID) -> VersionRef | None:
        if not self._same_client() or self.only_versions is not None:
            return None
        session = self.svc.session
        art = repository.get_artifact(session, self.tenant_id, artifact_id)
        if art is None or art["latest_published_version_id"] is None:
            return None
        row = repository.get_version(session, self.tenant_id, art["latest_published_version_id"])
        return self._ref(art, row) if row is not None else None

    def model_problems(self, model_ids: list[UUID]) -> dict[UUID, str]:
        if not self._same_client():
            return dict.fromkeys(model_ids, "model_not_found")
        # the client's own models plus shared ones only while its SHI switch is on, the
        # allowlist chain at the artifact's scope, and the second tenant check: all in
        # `problems_at` (models_admin), whose scope is this request's
        return self.svc.models.problems_at(self.svc.scope_ref_of(self.art), model_ids)

    def route_model_ids(self, route_id: UUID) -> list[UUID] | None:
        if not self._same_client():
            return None
        return self.svc.models.route_model_ids(route_id)

    def _readable_connection(self, connection_id: UUID) -> Mapping[str, Any] | None:
        """The client's connection row, if the caller may read it (or an approver re-validates)."""
        if not self._same_client():
            return None
        try:
            row = self.svc.conns.get_row(connection_id)  # the scope's client id, else NotFound
        except NotFound:
            return None
        if row["tenant_id"] != self.tenant_id:
            return None
        if (
            self.check_visibility
            and not self.svc.sc.pdp.authorize("connection:read", self.svc.conns.ref_of(row)).allowed
        ):
            return None  # no oracle for connections the author cannot read
        return row

    def connection(self, connection_id: UUID) -> ConnectionInfo | None:
        row = self._readable_connection(connection_id)
        if row is None:
            return None
        desc = self.svc.runtime.registry.descriptor(row["adapter_key"])
        return ConnectionInfo(
            id=row["id"],
            connection_type=row["connection_type"],
            adapter_key=row["adapter_key"],
            status=row["status"],
            access_mode=row["access_mode"],
            # at approval the use check was made for the requester at request time
            usable_by_publisher=(
                not self.check_visibility
                or self.svc.sc.pdp.authorize("connection:use", self.svc.conns.ref_of(row)).allowed
            ),
            implemented=bool(desc and desc.implemented),
            capabilities=frozenset(str(c) for c in desc.capabilities) if desc else frozenset(),
        )

    def schema_discovery(self, connection_id: UUID) -> dict[str, Any] | None:
        row = self._readable_connection(connection_id)  # only a connection shown readable
        if row is None:
            return None
        try:
            found = self.svc.conns.discovery_of(row["id"])
        except AppError:
            return None
        if found is None or found.kind != "schema" or not isinstance(found.payload, dict):
            return None
        return dict(found.payload)

    def mcp_tool(self, connection_id: UUID, tool_name: str) -> McpDiscovery:
        if not self._same_client():
            return McpDiscovery(False, None)
        try:
            # the scope's client id and `connection:read`, else an AppError
            found = self.svc.conns.latest_discovery(connection_id)
        except AppError:
            return McpDiscovery(False, None)
        if found.kind != "mcp_tools":
            return McpDiscovery(False, None)
        tools = found.payload.get("tools") if isinstance(found.payload, dict) else None
        for tool in tools or []:
            if isinstance(tool, dict) and tool.get("name") == tool_name:
                return McpDiscovery(True, tool)
        return McpDiscovery(True, None)

    def executor_maturity(self, executor: str) -> str:
        return self.svc.executor_maturities().get(executor, "planned")

    def permits(self, action: str, ref: VersionRef) -> bool:
        """`action` on the artifact `ref` names, decided at the artifact's own workspace and
        project as stored (never the ones `ref` carries): a forged or foreign `VersionRef`, or one
        whose scope does not match the row, gets False."""
        if not self._same_client():
            return False
        # a run's env permits only what the run executes (runtime isolation review 3 item 2)
        if self.only_versions is not None and ref.version_id not in self.only_versions:
            return False
        art = repository.get_artifact(self.svc.session, self.tenant_id, ref.artifact_id)
        if (
            art is None
            or art["tenant_id"] != self.tenant_id
            or art["kind"] != ref.kind
            or art["workspace_id"] != ref.workspace_id
            or art["project_id"] != ref.project_id
        ):
            return False
        if not self.check_visibility:  # an approver re-validates; the requester was checked
            return True
        return self.svc.sc.pdp.authorize(action, self.svc.ref_of(art)).allowed
