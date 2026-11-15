"""Routes of the P7 ontology query, policy and lineage API (docs/api/PHASE_7_CONTRACT.md sections
3-4). The ontology artifact CRUD / versions / mappings live in the build area (one artifact
mechanism); the action invoke route lives with the runtime (it creates a run)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import Response

from shifde.adapters.deps import RuntimeDep
from shifde.build.service import ArtifactService
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.ontology import schemas as s
from shifde.ontology.service import OntologyService
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1/orgs/{org}", tags=["ontology"])


def _service(scope: ScopeDep, runtime: RuntimeDep) -> OntologyService:
    return OntologyService(ArtifactService(scope, runtime))


Svc = Annotated[OntologyService, Depends(_service)]
PrincipalType = Annotated[str, Path(pattern="^(user|service_account)$")]


@router.post("/ontologies/{ontology_id}/query")
def query(svc: Svc, ontology_id: UUID, body: s.QueryIn) -> s.QueryOut:
    return svc.query(ontology_id, body)


@router.get("/ontologies/{ontology_id}/objects/{object_type}/{pk}")
def get_object(
    svc: Svc,
    ontology_id: UUID,
    object_type: Annotated[str, Path(min_length=1, max_length=200)],
    pk: Annotated[str, Path(min_length=1, max_length=500)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> s.ObjectOut:
    return svc.get_object(ontology_id, object_type, pk, version_id=version_id)


@router.get("/ontologies/{ontology_id}/policies")
def list_policies(svc: Svc, ontology_id: UUID) -> list[s.PolicyOut]:
    return svc.list_policies(ontology_id)


@router.post("/ontologies/{ontology_id}/policies", status_code=201)
def create_policy(svc: Svc, ontology_id: UUID, body: s.PolicyIn) -> s.PolicyOut:
    return svc.create_policy(ontology_id, body)


@router.patch("/ontologies/{ontology_id}/policies/{policy_id}")
def update_policy(svc: Svc, ontology_id: UUID, policy_id: UUID, body: s.PolicyIn) -> s.PolicyOut:
    return svc.update_policy(ontology_id, policy_id, body)


@router.delete("/ontologies/{ontology_id}/policies/{policy_id}", status_code=204)
def delete_policy(svc: Svc, ontology_id: UUID, policy_id: UUID) -> Response:
    svc.delete_policy(ontology_id, policy_id)
    return Response(status_code=204)


@router.get("/ontology-lineage")
def list_lineage(
    svc: Svc,
    ontology_id: Annotated[UUID, Query()],
    run_id: Annotated[UUID | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.LineageOut]:
    return svc.list_lineage(ontology_id, run_id, limit, cursor)


@router.get("/ontology-principal-attributes")
def list_attributes(svc: Svc) -> list[s.AttributesOut]:
    return svc.list_attributes()


@router.get("/ontology-principal-attributes/{principal_type}/{principal_id}")
def get_attributes(svc: Svc, principal_type: PrincipalType, principal_id: UUID) -> s.AttributesOut:
    return svc.get_attributes(principal_type, principal_id)


@router.put("/ontology-principal-attributes/{principal_type}/{principal_id}")
def put_attributes(
    svc: Svc, principal_type: PrincipalType, principal_id: UUID, body: s.AttributesIn
) -> s.AttributesOut:
    return svc.put_attributes(principal_type, principal_id, body)


@router.put("/ontologies/{ontology_id}/materializations/{object_type}", status_code=501)
def materialize(
    svc: Svc, ontology_id: UUID, object_type: Annotated[str, Path(min_length=1, max_length=200)]
) -> None:
    """Materialization is off in the MVP: always 501 `materialization_not_enabled`."""
    svc.materialize(ontology_id)
