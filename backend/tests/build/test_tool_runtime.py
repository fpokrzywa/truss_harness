"""build part 2b: tool execution (`build/tool_runtime.py`).

The two SETH `test_p4_review1.py` tests that call only `tool_runtime` (`test_path_encoding_helpers`,
`test_side_effects_are_never_retried_at_runtime`) are copied with unchanged assertions. The rest
drives `tool_runtime.execute` / `invoke_once` through the real `ConnectionService` of the ported
connections module, in the context of one client, against local listeners, and proves that the tool
path is no way to another client's connection, secret or host.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.runtime import IntegrationRuntime
from shifde.adapters.testservers import RestTestServer
from shifde.authz.service import new_pdp
from shifde.build import tool_runtime
from shifde.connections.service import ConnectionService
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database, set_principal, set_tenant
from shifde.core.errors import AppError, Forbidden, NotFound
from shifde.core.settings import Settings
from shifde.tenancy.scope import TenantScope
from tests.build.conftest import World
from tests.connections.conftest import client, people, rest_server
from tests.connections.routes import (
    REGISTRY,
    REST_TOKEN,
    activate,
    as_user,
    connection,
    org,
    rest_body,
    secret,
)
from tests.tenancy.conftest import app_session
from tests.tenancy.routes import People

__all__ = ["client", "people", "rest_server"]


# ------------------------------------------------------------------ SETH assertions, unchanged
def test_path_encoding_helpers() -> None:
    assert (
        tool_runtime.fill_path("/files/{n}/x", {"n": "a b/c?d#e"}) == "/files/a%20b%2Fc%3Fd%23e/x"
    )
    assert tool_runtime.fill_path("/v/{n}", {"n": 5}) == "/v/5"
    for bad in (".", ".."):
        with pytest.raises(AppError):
            tool_runtime.fill_path("/v/{n}", {"n": bad})
    req = tool_runtime.rest_request(
        {
            "method": "post",
            "path_template": "/items/{id}",
            "query_template": {"q": "{q}", "fixed": "1", "opt": "{missing}"},
            "body_template": {"a": "{n}", "s": "x-{id}", "skip": "{missing}"},
        },
        {"id": "7", "q": "a&b", "n": 3},
        "key-1",
    )
    assert req == {
        "method": "POST",
        "path": "/items/7",
        "query": {"q": "a&b", "fixed": "1"},
        "body": {"a": 3, "s": "x-7"},
        "headers": {"Idempotency-Key": "key-1"},
    }


class _FailingConns:
    def __init__(self) -> None:
        self.calls = 0

    @contextmanager
    def open_for_use(self, *_a: Any, **_k: Any) -> Iterator[Any]:
        self.calls += 1
        raise AdapterFailure(ErrorClass.NETWORK, "boom")
        yield  # pragma: no cover


def test_side_effects_are_never_retried_at_runtime() -> None:
    def spec(side: str) -> dict[str, Any]:
        return {
            "executor": "rest.http",
            "binding": {
                "connection_id": str(uuid.uuid4()),
                "method": "POST",
                "path_template": "/x",
            },
            "side_effect_class": side,
            "timeout_s": 5,
            "retry": {"max_attempts": 3, "backoff_s": 0},
            "idempotency": "none",
        }

    for side, expected in (("write_non_idempotent", 1), ("destructive", 1), ("read", 3)):
        conns = _FailingConns()
        with pytest.raises(AppError):
            tool_runtime.execute(
                cast(ConnectionService, conns),
                spec(side),
                {},
                purpose="t",
                idempotency_key="k",
                sleep=lambda _s: None,
            )
        assert conns.calls == expected, side


# ------------------------------------------------------------------ the connection path
@dataclass(frozen=True)
class Side:
    tenant: UUID
    slug: str
    email: str
    connection: UUID
    secret: UUID
    server: RestTestServer
    seen: int = 0  # secret-access rows the set-up itself wrote (its health checks)


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side
    viewer_a: str


def _side(
    client: TestClient,
    app_db: Database,
    settings: Settings,
    tenant: UUID,
    slug: str,
    email: str,
    server: RestTestServer,
) -> Side:
    base, h = org(slug), as_user(client, email)
    sid = secret(app_db, settings, tenant, REST_TOKEN)
    conn = connection(client, h, rest_body(server, sid, name=f"rest-{slug}"), base)
    activate(client, h, conn["id"], base)
    return Side(tenant, slug, email, UUID(conn["id"]), UUID(sid), server)


@pytest.fixture
def b_server() -> Iterator[RestTestServer]:
    with RestTestServer(token=REST_TOKEN) as server:
        yield server


@pytest.fixture
def two(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
    b_server: RestTestServer,
) -> Two:
    b = _side(client, app_db, settings, world.b.tenant, world.b.slug, people.owner_b, b_server)
    a = _side(client, app_db, settings, world.a.tenant, world.a.slug, people.owner_a, rest_server)
    for server in (rest_server, b_server):
        server.requests.clear()  # the set-up's own health checks
    a, b = (replace(x, seen=_count(owner_db, x.secret)) for x in (a, b))
    return Two(a, b, people.viewer_a)


@contextmanager
def _service(
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    side: Side,
    email: str,
    *,
    as_owner: bool = False,
) -> Iterator[ConnectionService]:
    """The connections service of `email` in `side`'s client, as `shifde_app` (row-level security
    applies) or, with `as_owner`, as the schema owner (it does not: only the tenant filter and the
    row check of the service confine the caller)."""
    with owner_db.session() as o:
        user = o.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
        ).scalar_one()
    principal = Principal(user_id=user, email=email, display_name=email)
    opened = owner_db.session() if as_owner else app_session(app_db, side.tenant, user)
    with opened as s:
        if as_owner:
            set_tenant(s, side.tenant)
            set_principal(s, user)
        scope = TenantScope(
            session=s,
            ctx=RequestContext(request_id="r-tool", principal=principal, tenant_id=side.tenant),
            principal=principal,
            tenant_id=side.tenant,
            tenant_slug=side.slug,
            tenant_name=side.slug,
            tenant_status="active",
            pdp=new_pdp(s, principal, side.tenant),
        )
        yield ConnectionService(scope, IntegrationRuntime(settings=settings, registry=REGISTRY))


def _spec(connection_id: UUID, side: str = "read", **extra: Any) -> dict[str, Any]:
    return {
        "executor": "rest.http",
        "binding": {
            "connection_id": str(connection_id),
            "method": "GET" if side == "read" else "POST",
            "path_template": "/echo",
            "query_template": {"n": "{n}"},
            **extra,
        },
        "side_effect_class": side,
        "timeout_s": 5,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "idempotency": "none",
    }


def _count(owner_db: Database, secret_id: UUID) -> int:
    with owner_db.session() as o:
        return int(
            o.execute(
                text("SELECT count(*) FROM shifde.secret_access_log WHERE secret_id = :s"),
                {"s": secret_id},
            ).scalar_one()
        )


def _accesses(owner_db: Database, side: Side) -> int:
    """Times the tool path resolved `side`'s secret (rows after the set-up)."""
    return _count(owner_db, side.secret) - side.seen


def _connection_row(owner_db: Database, connection_id: UUID) -> tuple[Any, ...]:
    with owner_db.session() as o:
        return tuple(
            o.execute(
                text(
                    "SELECT config, secret_ref_id, row_version, status, access_mode "
                    "FROM shifde.connections WHERE id = :i"
                ),
                {"i": connection_id},
            ).one()
        )


def _run(conns: ConnectionService, call: str, spec: dict[str, Any]) -> Any:
    if call == "execute":
        return tool_runtime.execute(conns, spec, {"n": 1}, purpose="t", idempotency_key="k")
    return tool_runtime.invoke_once(
        conns, spec, {"n": 1}, purpose="t", idempotency_key="k", deadline=5.0
    )


def _refusal(conns: ConnectionService, call: str, spec: dict[str, Any]) -> tuple[type, str]:
    with pytest.raises(AppError) as raised:
        _run(conns, call, spec)
    return type(raised.value), str(raised.value)


@pytest.mark.parametrize("call", ["execute", "invoke_once"])
def test_a_tool_runs_through_its_own_clients_connection_and_secret(
    call: str, two: Two, app_db: Database, owner_db: Database, settings: Settings
) -> None:
    a, b = two.a, two.b
    with _service(app_db, owner_db, settings, a, a.email) as conns:
        out = _run(conns, call, _spec(a.connection))
    status = out.output["status"] if call == "execute" else out["status"]
    assert status == 200
    assert [r["path"] for r in a.server.requests] == ["/echo"]
    assert a.server.requests[0]["authenticated"] is True  # A's own secret reached A's own host
    assert _accesses(owner_db, a) >= 1
    assert b.server.requests == [] and _accesses(owner_db, b) == 0


@pytest.mark.parametrize("as_owner", [False, True], ids=["as-app-role", "as-owner-no-rls"])
@pytest.mark.parametrize("call", ["execute", "invoke_once"])
def test_a_tool_naming_bs_connection_gets_the_unknown_id_outcome(
    call: str,
    as_owner: bool,
    two: Two,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
) -> None:
    a, b = two.a, two.b
    before = _connection_row(owner_db, b.connection)
    with _service(app_db, owner_db, settings, a, a.email, as_owner=as_owner) as conns:
        theirs = _refusal(conns, call, _spec(b.connection))
        unknown = _refusal(conns, call, _spec(uuid.uuid4()))
    assert theirs == unknown and theirs[0] is NotFound, (theirs, unknown)
    assert b.server.requests == [] and a.server.requests == []  # nothing was sent anywhere
    assert _accesses(owner_db, b) == 0  # B's secret was not resolved
    assert _connection_row(owner_db, b.connection) == before


def test_the_connection_is_looked_up_in_the_session_client_not_the_spec(
    two: Two, app_db: Database, owner_db: Database, settings: Settings
) -> None:
    """B's own context finds B's connection (control). A client id carried by the spec changes
    nothing: A's context with a spec that claims B's client still gets the unknown-id outcome."""
    a, b = two.a, two.b
    with _service(app_db, owner_db, settings, b, b.email) as conns:
        assert _run(conns, "execute", _spec(b.connection)).output["status"] == 200
    assert [r["authenticated"] for r in b.server.requests] == [True]
    claimed = {**_spec(b.connection, tenant_id=str(b.tenant)), "tenant_id": str(b.tenant)}
    with _service(app_db, owner_db, settings, a, a.email) as conns:
        assert _refusal(conns, "execute", claimed)[0] is NotFound
    assert a.server.requests == [] and len(b.server.requests) == 1


def test_binding_fields_cannot_redirect_a_call_to_another_host(
    two: Two, app_db: Database, owner_db: Database, settings: Settings
) -> None:
    """Only the connection decides the destination (C2): a binding that names a host, a base URL
    or a secret sends the call to the connection's own host with the connection's own secret, and
    leaves the connection untouched."""
    a, b = two.a, two.b
    before = _connection_row(owner_db, a.connection)
    spec = _spec(
        a.connection,
        base_url=b.server.base_url,
        url=b.server.base_url + "/echo",
        host="127.0.0.1",
        secret_id=str(b.secret),
    )
    with _service(app_db, owner_db, settings, a, a.email) as conns:
        _run(conns, "execute", spec)
    assert len(a.server.requests) == 1 and b.server.requests == []
    assert _accesses(owner_db, b) == 0
    assert _connection_row(owner_db, a.connection) == before


@pytest.mark.parametrize("side", sorted(tool_runtime.WRITE_CLASSES))
def test_a_write_tool_needs_a_read_write_connection(
    side: str, two: Two, app_db: Database, owner_db: Database, settings: Settings
) -> None:
    a = two.a  # A's connection is read-only
    with _service(app_db, owner_db, settings, a, a.email) as conns:
        assert _refusal(conns, "execute", _spec(a.connection, side))[0] is Forbidden
        assert _refusal(conns, "invoke_once", _spec(a.connection, side))[0] is Forbidden
        # control: the same connection serves a read class
        assert _run(conns, "execute", _spec(a.connection)).output["status"] == 200
    assert [r["method"] for r in a.server.requests] == ["GET"]  # no write reached the target


@pytest.mark.parametrize("side", ["read", "write_idempotent"])
def test_a_principal_without_connection_use_is_refused_before_any_call(
    side: str, two: Two, app_db: Database, owner_db: Database, settings: Settings
) -> None:
    a = two.a
    with _service(app_db, owner_db, settings, a, two.viewer_a) as conns:
        refused = _refusal(conns, "execute", _spec(a.connection, side))
    assert refused[0] in (Forbidden, NotFound)
    assert a.server.requests == [] and _accesses(owner_db, a) == 0
