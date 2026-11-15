"""Routes of the P4 build area (contract sections 1-5). The four artifact kinds share one route
set; kind-specific routes (render/test/status/resolved) are added per kind. Routes are registered
per kind with a literal path prefix (never `/{kind}`), so they cannot shadow other paths."""

from __future__ import annotations

from typing import Annotated, get_args
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse, Response

from shifde.adapters.deps import RuntimeDep
from shifde.build import catalog, kind_ops, ontology_ops
from shifde.build import schemas as s
from shifde.build.service import ArtifactService
from shifde.core.deps import SessionDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")
org_router = APIRouter(prefix="/orgs/{org}")


def _service(scope: ScopeDep, runtime: RuntimeDep) -> ArtifactService:
    return ArtifactService(scope, runtime)


Svc = Annotated[ArtifactService, Depends(_service)]


def _kind_router(kind: s.Kind) -> APIRouter:
    r = APIRouter(prefix=f"/{kind}", tags=[kind])

    @r.get("")
    def list_artifacts(
        svc: Svc,
        scope_type: Annotated[s.ScopeType | None, Query()] = None,
        scope_id: Annotated[UUID | None, Query()] = None,
        status: Annotated[s.ArtifactStatus | None, Query()] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        limit: LimitQuery = DEFAULT_LIMIT,
        cursor: CursorQuery = None,
    ) -> Page[s.ArtifactOut]:
        return svc.list_artifacts(
            kind,
            scope_type=scope_type,
            scope_id=scope_id,
            status=status,
            q=q,
            limit=limit,
            cursor=cursor,
        )

    @r.post("", status_code=201)
    def create(svc: Svc, body: s.ArtifactCreate) -> s.ArtifactOut:
        return svc.create(kind, body)

    @r.get("/{artifact_id}")
    def get(svc: Svc, artifact_id: UUID) -> s.ArtifactOut:
        return svc.get(kind, artifact_id)

    @r.patch("/{artifact_id}")
    def update(svc: Svc, artifact_id: UUID, body: s.ArtifactPatch) -> s.ArtifactOut:
        return svc.update(kind, artifact_id, body)

    @r.get("/{artifact_id}/versions")
    def list_versions(svc: Svc, artifact_id: UUID) -> list[s.VersionOut]:
        return svc.list_versions(kind, artifact_id)

    @r.get("/{artifact_id}/versions/{version_id}")
    def get_version(svc: Svc, artifact_id: UUID, version_id: UUID) -> s.VersionOut:
        return svc.get_version(kind, artifact_id, version_id)

    @r.post("/{artifact_id}/versions/{version_id}/state")
    def set_state(svc: Svc, artifact_id: UUID, version_id: UUID, body: s.StateIn) -> s.VersionOut:
        return svc.set_state(kind, artifact_id, version_id, body)

    @r.get("/{artifact_id}/draft")
    def get_draft(svc: Svc, artifact_id: UUID) -> s.VersionOut:
        return svc.get_draft(kind, artifact_id)

    @r.put("/{artifact_id}/draft")
    def put_draft(svc: Svc, artifact_id: UUID, body: s.DraftPut) -> s.VersionOut:
        return svc.put_draft(kind, artifact_id, body)

    @r.post("/{artifact_id}/draft/validate", response_model_exclude_none=True)
    def validate_draft(svc: Svc, artifact_id: UUID) -> s.ValidationOut:
        return svc.validate_draft(kind, artifact_id)

    @r.post(
        "/{artifact_id}/publish",
        response_model=s.VersionOut,
        responses={202: {"model": s.PublishPendingOut, "description": "approval required"}},
    )
    def publish(svc: Svc, artifact_id: UUID, body: s.PublishIn) -> Response:
        result = svc.publish(kind, artifact_id, body)
        if isinstance(result, s.PublishPendingOut):
            return JSONResponse(status_code=202, content=result.model_dump(mode="json"))
        return JSONResponse(content=result.model_dump(mode="json"))

    @r.get("/{artifact_id}/diff")
    def diff(
        svc: Svc,
        artifact_id: UUID,
        from_: Annotated[str, Query(alias="from", min_length=1, max_length=64)],
        to: Annotated[str, Query(min_length=1, max_length=64)],
    ) -> s.DiffOut:
        return svc.diff(kind, artifact_id, from_, to)

    @r.post("/{artifact_id}/rollback")
    def rollback(svc: Svc, artifact_id: UUID, body: s.RollbackIn) -> s.VersionOut:
        return svc.rollback(kind, artifact_id, body)

    if kind == "prompts":

        @r.post("/{artifact_id}/render")
        def render(svc: Svc, artifact_id: UUID, body: s.RenderIn) -> s.RenderOut:
            return kind_ops.PromptOps(svc).render(artifact_id, body)

        @r.post("/{artifact_id}/test")
        def test_prompt(svc: Svc, artifact_id: UUID, body: s.PromptTestIn) -> s.PromptTestRunOut:
            return kind_ops.PromptOps(svc).test(artifact_id, body)

        @r.get("/{artifact_id}/test-runs")
        def test_runs(
            svc: Svc,
            artifact_id: UUID,
            limit: LimitQuery = DEFAULT_LIMIT,
            cursor: CursorQuery = None,
        ) -> Page[s.PromptTestRunOut]:
            return kind_ops.PromptOps(svc).test_runs(artifact_id, limit, cursor)

    if kind == "tools":

        @r.post("/{artifact_id}/test")
        def test_tool(svc: Svc, artifact_id: UUID, body: s.ToolTestIn) -> s.ToolTestOut:
            return kind_ops.ToolOps(svc).test(artifact_id, body)

    if kind == "agents":

        @r.post("/{artifact_id}/test")
        def test_agent(svc: Svc, artifact_id: UUID, body: s.AgentTestIn) -> s.AgentTestOut:
            return kind_ops.AgentOps(svc).test(artifact_id, body)

        @r.post("/{artifact_id}/status")
        def set_status(svc: Svc, artifact_id: UUID, body: s.AgentStatusIn) -> s.ArtifactOut:
            return kind_ops.AgentOps(svc).set_status(artifact_id, body)

        @r.get("/{artifact_id}/versions/{version_id}/resolved")
        def resolved(svc: Svc, artifact_id: UUID, version_id: UUID) -> s.ResolvedOut:
            return kind_ops.AgentOps(svc).resolved(artifact_id, version_id)

    if kind == "ontologies":

        @r.get("/{artifact_id}/versions/{version_id}/mappings")
        def list_mappings(svc: Svc, artifact_id: UUID, version_id: str) -> list[s.MappingOut]:
            return ontology_ops.OntologyMappingOps(svc).list(artifact_id, version_id)

        @r.put("/{artifact_id}/draft/mappings/{target_kind}/{target_key}")
        def put_mapping(
            svc: Svc,
            artifact_id: UUID,
            target_kind: s.MappingTargetKind,
            target_key: Annotated[str, Path(min_length=1, max_length=200)],
            body: s.MappingIn,
        ) -> s.MappingOut:
            return ontology_ops.OntologyMappingOps(svc).put(
                artifact_id, target_kind, target_key, body
            )

        @r.delete("/{artifact_id}/draft/mappings/{target_kind}/{target_key}", status_code=204)
        def delete_mapping(
            svc: Svc,
            artifact_id: UUID,
            target_kind: s.MappingTargetKind,
            target_key: Annotated[str, Path(min_length=1, max_length=200)],
        ) -> Response:
            ontology_ops.OntologyMappingOps(svc).delete(artifact_id, target_kind, target_key)
            return Response(status_code=204)

        @r.post("/{artifact_id}/draft/mappings/{target_kind}/{target_key}/check")
        def check_mapping(
            svc: Svc,
            artifact_id: UUID,
            target_kind: s.MappingTargetKind,
            target_key: Annotated[str, Path(min_length=1, max_length=200)],
        ) -> s.MappingCheckOut:
            return ontology_ops.OntologyMappingOps(svc).check(artifact_id, target_kind, target_key)

    return r


for _kind in get_args(s.Kind):
    org_router.include_router(_kind_router(_kind))


@org_router.get("/publish-requests", tags=["publish-requests"])
def list_publish_requests(
    svc: Svc,
    status: Annotated[s.PublishRequestStatus | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.PublishRequestOut]:
    return svc.list_requests(status, limit, cursor)


@org_router.post("/publish-requests/{request_id}/decide", tags=["publish-requests"])
def decide_publish_request(svc: Svc, request_id: UUID, body: s.DecideIn) -> s.PublishRequestOut:
    return svc.decide(request_id, body)


router.include_router(org_router)


@router.get("/tool-executors", tags=["tools"])
def tool_executors(session: SessionDep, runtime: RuntimeDep) -> s.ExecutorCatalogOut:
    return catalog.executor_catalog(session, runtime.registry)


@router.get("/pipelines/node-types", tags=["pipelines"])
def pipeline_node_types(session: SessionDep, runtime: RuntimeDep) -> s.NodeTypeCatalogOut:
    return catalog.node_type_catalog(session, runtime.registry)
