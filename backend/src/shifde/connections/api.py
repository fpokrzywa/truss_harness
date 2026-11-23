"""Connection, egress-policy and platform egress-default routes (contract P3 section 4)."""

from __future__ import annotations

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query
from fastapi.responses import JSONResponse

from shifde.adapters.base import AdapterRules
from shifde.adapters.deps import RuntimeDep
from shifde.connections import egress
from shifde.connections import schemas as s
from shifde.connections.service import ConnectionService
from shifde.core.deps import RequestCtxDep, SessionDep
from shifde.core.errors import ValidationFailed
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.identity.dependencies import AuthDep
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")
IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,200}$")  # as `runtime.api` (POST /runs)


def _connections(scope: ScopeDep, runtime: RuntimeDep) -> ConnectionService:
    rules = AdapterRules(
        databricks_host_suffixes=tuple(runtime.settings.delivery_databricks_host_suffixes)
    )
    return ConnectionService(scope, runtime, rules)


def _policies(scope: ScopeDep) -> egress.EgressPolicyService:
    return egress.EgressPolicyService(scope)


Connections = Annotated[ConnectionService, Depends(_connections)]
Policies = Annotated[egress.EgressPolicyService, Depends(_policies)]


# ---------------------------------------------------------------- connections
@router.get("/orgs/{org}/connections", tags=["connections"])
def list_connections(
    svc: Connections,
    type: Annotated[str | None, Query()] = None,  # noqa: A002 - contract query name
    status: Annotated[str | None, Query()] = None,
    workspace: Annotated[str | None, Query()] = None,
    project: Annotated[str | None, Query()] = None,
    include_shared: Annotated[bool, Query()] = False,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.ConnectionOut]:
    return svc.list(
        connection_type=type,
        status=status,
        workspace=workspace,
        project=project,
        include_shared=include_shared,
        limit=limit,
        cursor=cursor,
    )


@router.post("/orgs/{org}/connections", status_code=201, tags=["connections"])
def create_connection(svc: Connections, body: s.ConnectionCreate) -> s.ConnectionOut:
    return svc.create(body)


@router.get("/orgs/{org}/connections/{connection_id}", tags=["connections"])
def get_connection(svc: Connections, connection_id: UUID) -> s.ConnectionOut:
    return svc.get(connection_id)


@router.patch("/orgs/{org}/connections/{connection_id}", tags=["connections"])
def update_connection(
    svc: Connections, connection_id: UUID, body: s.ConnectionPatch
) -> s.ConnectionOut:
    return svc.update(connection_id, body)


@router.put("/orgs/{org}/connections/{connection_id}/credential", tags=["connections"])
def set_connection_credential(
    svc: Connections, connection_id: UUID, body: s.ConnectionCredentialSet
) -> s.ConnectionOut:
    return svc.set_credential(connection_id, body)


@router.post("/orgs/{org}/connections/{connection_id}/status", tags=["connections"])
def set_connection_status(
    svc: Connections, connection_id: UUID, body: s.ConnectionStatusChange
) -> s.ConnectionOut:
    return svc.set_status(connection_id, body)


@router.post("/orgs/{org}/connections/{connection_id}/test", status_code=202, tags=["connections"])
def test_connection(svc: Connections, connection_id: UUID) -> s.TestJobOut:
    return svc.enqueue_test(connection_id)


@router.get("/orgs/{org}/connections/{connection_id}/test-jobs/{job_id}", tags=["connections"])
def get_connection_test_job(svc: Connections, connection_id: UUID, job_id: UUID) -> s.TestJobOut:
    return svc.test_job(connection_id, job_id)


@router.get("/orgs/{org}/connections/{connection_id}/tests", tags=["connections"])
def list_connection_tests(
    svc: Connections,
    connection_id: UUID,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.TestResultOut]:
    return svc.list_tests(connection_id, limit, cursor)


@router.post("/orgs/{org}/connections/{connection_id}/discover", tags=["connections"])
def discover_connection(svc: Connections, connection_id: UUID) -> s.DiscoveryOut:
    return svc.discover(connection_id)


@router.get("/orgs/{org}/connections/{connection_id}/discoveries/latest", tags=["connections"])
def latest_discovery(svc: Connections, connection_id: UUID) -> s.DiscoveryOut:
    return svc.latest_discovery(connection_id)


@router.post(
    "/orgs/{org}/connections/{connection_id}/invoke",
    status_code=202,
    response_model=s.InvocationOut,
    tags=["connections"],
)
def invoke_connection(
    svc: Connections,
    connection_id: UUID,
    body: Annotated[dict[str, Any], Body()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    """Queue the invoke on the worker (issue #190): 202 with the new invocation, 200 with the
    first one on a replay of the same `Idempotency-Key` by the same caller."""
    if idempotency_key is not None and not IDEMPOTENCY_KEY.match(idempotency_key):
        raise ValidationFailed("Idempotency-Key must be 1-200 printable ASCII characters")
    out, created = svc.enqueue_invoke(connection_id, body, idempotency_key)
    return JSONResponse(out.model_dump(mode="json"), status_code=202 if created else 200)


@router.get(
    "/orgs/{org}/connections/{connection_id}/invocations/{invocation_id}", tags=["connections"]
)
def get_connection_invocation(
    svc: Connections, connection_id: UUID, invocation_id: UUID
) -> s.InvocationOut:
    return svc.invocation(connection_id, invocation_id)


# ---------------------------------------------------------------- egress policies
@router.get("/orgs/{org}/egress-policies", tags=["egress"])
def list_egress_policies(
    svc: Policies, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.EgressPolicyOut]:
    return svc.list(limit, cursor)


@router.post("/orgs/{org}/egress-policies", status_code=201, tags=["egress"])
def create_egress_policy(svc: Policies, body: s.EgressPolicyCreate) -> s.EgressPolicyOut:
    return svc.create(body)


@router.get("/orgs/{org}/egress-policies/{policy_id}", tags=["egress"])
def get_egress_policy(svc: Policies, policy_id: UUID) -> s.EgressPolicyOut:
    return svc.get(policy_id)


@router.patch("/orgs/{org}/egress-policies/{policy_id}", tags=["egress"])
def patch_egress_policy(
    svc: Policies, policy_id: UUID, body: s.EgressPolicyPatch
) -> s.EgressPolicyOut:
    return svc.patch(policy_id, body)


@router.get("/platform/egress-default", tags=["egress"])
def get_egress_default(auth: AuthDep, session: SessionDep, ctx: RequestCtxDep) -> s.EgressPolicyOut:
    return egress.get_platform_default(session, ctx, auth.principal)


@router.put("/platform/egress-default", tags=["egress"])
def put_egress_default(
    auth: AuthDep, session: SessionDep, ctx: RequestCtxDep, body: s.EgressRules
) -> s.EgressPolicyOut:
    return egress.put_platform_default(session, ctx, auth.principal, body)
