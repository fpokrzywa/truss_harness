"""Issue #373: the routes that create a run refuse an `Idempotency-Key` that starts with the
`assist:` prefix of the platform's own runs (422 `idempotency_key_reserved`), before any lookup or
write. The match is exact and case sensitive; a malformed key answers as before. Each test has a
database of its own (the full-app harness of `tests/app`)."""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A, B
from tests._support.p4util import art, publish
from tests._support.p5util import Stack, rest_connection, rest_tool_spec
from tests._support.p7util import ACTION, World7, reference_spec
from tests.app.conftest import (
    _admin_engine,
    _dropper,
    app_db,
    app_url,
    client,
    owner_db,
    owner_engine,
    owner_session,
    owner_url,
    rest_server,
    test_database,
    world,
)

__all__ = [
    "_admin_engine",
    "_dropper",
    "app_db",
    "app_url",
    "client",
    "owner_db",
    "owner_engine",
    "owner_session",
    "owner_url",
    "rest_server",
    "test_database",
    "world",
]

RESERVED = ["assist:0190a1b2-0000-7000-8000-000000000001", "assist:anything"]
VARYING = {"instance", "request_id"}


def _runs(owner: Session) -> int:
    owner.rollback()
    count: int = owner.execute(text("SELECT count(*) FROM shifde.runs")).scalar_one()
    return count


def _post(
    c: TestClient, h: dict[str, str], path: str, body: dict[str, Any], key: str, org: str = A
) -> httpx.Response:
    return c.post(f"{org}{path}", headers={**h, "Idempotency-Key": key}, json=body)


def _answer(r: httpx.Response) -> tuple[int, dict[str, Any]]:
    return r.status_code, {k: v for k, v in r.json().items() if k not in VARYING}


def _assert_reserved(r: httpx.Response, key: str) -> None:
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "idempotency_key_reserved"
    assert r.json()["detail"] == "Idempotency-Key must not start with assist:"
    assert key not in r.text


def _run_body(c: TestClient, h: dict[str, str], owner: Session) -> dict[str, Any]:
    """The body of a run of a published agent."""
    stack = Stack(c, h, owner, script=[])
    return {
        "kind": "agent",
        "agent_id": stack.agent["id"],
        "workspace": "main",
        "project": "sample",
        "input": "hello",
    }


@pytest.mark.parametrize("key", RESERVED)
def test_a_run_with_a_reserved_key_is_refused(
    client: TestClient, world: World, owner_session: Session, key: str
) -> None:
    h = login(client, world.passwords, ADMIN)
    body = _run_body(client, h, owner_session)
    _assert_reserved(_post(client, h, "/runs", body, key), key)
    assert _runs(owner_session) == 0
    # positive control: the same body with an ordinary key
    assert _post(client, h, "/runs", body, "k-1").status_code == 202
    assert _runs(owner_session) == 1
    # a run that already has exactly this key is not replayed
    owner_session.execute(
        text("UPDATE shifde.runs SET idempotency_key = :k WHERE idempotency_key = 'k-1'"),
        {"k": key},
    )
    owner_session.commit()
    _assert_reserved(_post(client, h, "/runs", body, key), key)
    assert _runs(owner_session) == 1


@pytest.mark.parametrize("key", RESERVED)
def test_an_action_invocation_with_a_reserved_key_is_refused(
    client: TestClient,
    world: World,
    owner_session: Session,
    sample_db_url: str,
    rest_server: RestTestServer,
    key: str,
) -> None:
    w = World7(client, world, owner_session, sample_db_url)
    rest_id = rest_connection(client, w.h, rest_server.base_url)
    spec = rest_tool_spec(rest_id, "POST", "/echo", "write_non_idempotent", body=True)
    tool = art(client, w.h, "tools", "escalate", spec, w.scope)
    tool_version = publish(client, w.h, "tools", tool["id"])
    onto = w.ready("customer-service", reference_spec(tool_version["id"]))
    w.allow_all(onto["id"], ("role", w.admin_role))
    path = f"/ontologies/{onto['id']}/actions/{ACTION}/invoke"
    body = {"workspace": "main", "project": "sample", "object_pk": "7"}
    _assert_reserved(_post(client, w.h, path, body, key), key)
    assert _runs(owner_session) == 0
    # the malformed-key answer of this route is unchanged
    malformed = _post(client, w.h, path, body, "a key")
    assert (malformed.status_code, malformed.json()["code"]) == (422, "validation_error")
    # positive control: the same body with an ordinary key
    made = _post(client, w.h, path, body, "k-1")
    assert made.status_code == 202, made.text
    assert _runs(owner_session) == 1


def test_keys_that_only_resemble_the_prefix_and_malformed_keys_answer_as_before(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    body = _run_body(client, h, owner_session)
    for key in ("Assist:x", "assist", "x-assist:1"):
        made = _post(client, h, "/runs", body, key)
        assert made.status_code == 202, (key, made.text)
    for key in ("a key", "x" * 201, "assist: x", "assist:" + "x" * 200):
        r = _post(client, h, "/runs", body, key)
        assert (r.status_code, r.json()["code"]) == (422, "validation_error"), (key, r.text)
        assert r.json()["detail"] == "Idempotency-Key must be 1-200 printable ASCII characters"
    assert _runs(owner_session) == 3


def test_a_reserved_key_does_not_change_the_answers_before_it(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """Another client's organization, an unknown one and a missing session answer as with an
    ordinary key."""
    h = login(client, world.passwords, ADMIN)
    run = _run_body(client, h, owner_session)
    invoke = {"workspace": "main", "project": "sample", "object_pk": "1"}
    requests = [("/runs", run), (f"/ontologies/{uuid4()}/actions/act/invoke", invoke)]
    for path, body in requests:
        for org in (B, "/api/v1/orgs/no-such-org"):
            refused = _answer(_post(client, h, path, body, RESERVED[0], org))
            assert refused == _answer(_post(client, h, path, body, "k-1", org))
            assert refused[0] == 404
    client.cookies.clear()
    for path, body in requests:
        assert _post(client, {}, path, body, RESERVED[0]).status_code == 401
    assert _runs(owner_session) == 0
