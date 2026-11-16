"""P4 review round 1: lost updates, jsonb-stable hashes, untrusted-in-system, visibility leaks,
approval edge cases, tool runtime (encoding, retries, idempotency keys)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.testservers import RestTestServer
from shifde.build.canonical import spec_hash
from tests._support.fullapp import World
from tests._support.p2util import login, new_user, post
from tests._support.p3util import ADMIN, A, fake_model_stack
from tests._support.p4util import (
    PASSWORD,
    agent_spec,
    art,
    as_user,
    builtin_tool_spec,
    codes,
    edit,
    make_user,
    prompt_spec,
    publish,
    set_tenant_setting,
    tenant_scope,
)
from tests.app.test_build_tools import rest_conn, rest_spec, run_tool, validate


def vcodes(client: TestClient, h: dict[str, str], kind: str, aid: str) -> list[str]:
    r = client.post(f"{A}/{kind}/{aid}/draft/validate", headers=h)
    assert r.status_code == 200, r.text
    return sorted(e["code"] for e in r.json()["errors"])


# ================================================================== 1. lost update
def test_publish_waits_for_a_concurrent_draft_writer_and_publishes_what_it_wrote(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    p = art(client, h, "prompts", "racy", prompt_spec(), tenant_scope(owner_session))
    written = prompt_spec(sections={"system": "written concurrently", "context": "{{message}}"})
    import json

    owner_session.execute(
        text("SELECT id FROM shifde.artifact_versions WHERE artifact_id = :a FOR UPDATE"),
        {"a": p["id"]},
    )  # an in-flight draft writer holds the row
    result: dict[str, Any] = {}

    def go() -> None:
        result["r"] = client.post(f"{A}/prompts/{p['id']}/publish", json={}, headers=h)

    thread = threading.Thread(target=go)
    thread.start()
    time.sleep(1.5)
    assert thread.is_alive()  # publish is waiting on the row lock, it did not read a stale draft
    owner_session.execute(
        text(
            "UPDATE shifde.artifact_versions SET spec = CAST(:s AS jsonb), "
            "row_version = row_version + 1 WHERE artifact_id = :a"
        ),
        {"s": json.dumps(written), "a": p["id"]},
    )
    owner_session.commit()
    thread.join(30)
    r = result["r"]
    assert r.status_code == 200, r.text
    assert r.json()["spec"]["sections"]["system"] == "written concurrently"
    assert r.json()["spec_hash"] == spec_hash(r.json()["spec"])
    # a writer holding the old row_version now fails instead of touching the published version
    edit(client, h, "prompts", p["id"], prompt_spec(), 1, status=409)


# ================================================================== 2. jsonb-stable hashes
def test_spec_hash_is_the_hash_of_the_stored_spec_and_approval_still_works(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    make_user(
        client, owner_session, h, "approver@demo-a.local", "tenant_administrator", world.passwords
    )
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    odd = prompt_spec(
        examples=[{"inputs": {"message": "x", "n": 1e16, "z": -0.0, "e": 1.5e-7}, "output": "o"}]
    )
    p = art(client, h, "prompts", "numbers", odd, scope)
    v = publish(client, h, "prompts", p["id"])
    stored = client.get(f"{A}/prompts/{p['id']}/versions/{v['id']}", headers=h).json()
    assert v["spec_hash"] == spec_hash(stored["spec"]) == spec_hash(v["spec"])
    row = owner_session.execute(
        text("SELECT spec FROM shifde.artifact_versions WHERE id = :i"), {"i": v["id"]}
    ).scalar_one()
    assert spec_hash(row) == v["spec_hash"]

    set_tenant_setting(owner_session, "demo-a", "publish_requires_approval", True)
    q = art(client, h, "prompts", "numbers-approved", odd, scope)
    req = client.post(f"{A}/prompts/{q['id']}/publish", json={}, headers=h).json()[
        "publish_request"
    ]
    approver = as_user(client, world.passwords, "approver@demo-a.local")
    done = client.post(
        f"{A}/publish-requests/{req['id']}/decide", json={"decision": "approve"}, headers=approver
    )
    assert done.status_code == 200, done.text
    ver = client.get(f"{A}/prompts/{q['id']}/versions/{req['version_id']}", headers=approver).json()
    assert ver["spec_hash"] == spec_hash(ver["spec"])


# ================================================================== 3. untrusted -> system
def test_untrusted_variables_never_reach_the_system_message(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    bad = prompt_spec(sections={"system": "Be kind to {{message}}", "context": "x"})
    p = art(client, h, "prompts", "sys-untrusted", bad, scope)
    assert vcodes(client, h, "prompts", p["id"]) == ["untrusted_in_system"]
    assert client.post(f"{A}/prompts/{p['id']}/publish", json={}, headers=h).status_code == 422
    r = client.post(
        f"{A}/prompts/{p['id']}/render",
        json={"version_id": "draft", "variables": {"message": "m"}},
        headers=h,
    )
    assert r.status_code == 422 and "untrusted_in_system" in codes(r.json())
    # a trusted variable may be used in the system section
    edit(
        client,
        h,
        "prompts",
        p["id"],
        prompt_spec(sections={"system": "In {{language}}", "context": "{{message}}"}),
        1,
    )
    assert vcodes(client, h, "prompts", p["id"]) == []

    # via composition: the composing prompt re-declares the child's trusted variable as untrusted
    child_vars = [
        {"name": "who", "type": "string", "required": False, "default": "x", "trusted": True}
    ]
    child = art(
        client,
        h,
        "prompts",
        "child-sys",
        prompt_spec(sections={"system": "Child {{who}}"}, variables=child_vars, examples=[]),
        scope,
    )
    child_v = publish(client, h, "prompts", child["id"])
    parent_vars = [
        {"name": "who", "type": "string", "required": False, "default": "y", "trusted": False}
    ]
    parent = art(
        client,
        h,
        "prompts",
        "parent-sys",
        prompt_spec(
            sections={"system": "Parent", "context": "{{who}}"},
            variables=parent_vars,
            examples=[],
            composed_of=[{"prompt_version_id": child_v["id"], "placement": "system_prefix"}],
        ),
        scope,
    )
    assert vcodes(client, h, "prompts", parent["id"]) == ["untrusted_in_system"]
    # the renderer refuses too, even for an already-published version (inserted around validation)
    owner_session.execute(
        text(
            "UPDATE shifde.artifact_versions SET state = 'published', spec_hash = 'x', "
            "published_at = now() WHERE artifact_id = :a"
        ),
        {"a": parent["id"]},
    )
    owner_session.commit()
    vid = owner_session.execute(
        text("SELECT id FROM shifde.artifact_versions WHERE artifact_id = :a"), {"a": parent["id"]}
    ).scalar_one()
    rr = client.post(
        f"{A}/prompts/{parent['id']}/render",
        json={"version_id": str(vid), "variables": {}},
        headers=h,
    )
    assert rr.status_code == 422 and codes(rr.json()) == {"untrusted_in_system"}


def test_run_input_cannot_feed_a_trusted_variable(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    model = fake_model_stack(client, h)["model"]
    spec = prompt_spec(
        sections={"system": "s", "context": "{{message}} {{language}}"},
        variables=[
            {"name": "message", "type": "string", "required": True},
            {"name": "language", "type": "string", "required": True, "trusted": True},
        ],
        examples=[],
    )
    p = art(client, h, "prompts", "two-vars", spec, scope)
    pv = publish(client, h, "prompts", p["id"])
    bad = agent_spec(
        pv["id"], model, variables_map={"message": "{{input}}", "language": "{{input.lang}}"}
    )
    a = art(client, h, "agents", "feeds-trusted", bad, scope)
    assert vcodes(client, h, "agents", a["id"]) == ["untrusted_into_trusted"]
    ok = agent_spec(pv["id"], model, variables_map={"message": "{{input}}", "language": "English"})
    edit(client, h, "agents", a["id"], ok, 1)
    assert vcodes(client, h, "agents", a["id"]) == []


# ================================================================== 5. pins redaction
def test_pins_of_invisible_artifacts_are_redacted(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    make_user(
        client, owner_session, h, "admin2@demo-a.local", "tenant_administrator", world.passwords
    )
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    model = fake_model_stack(client, h)["model"]
    secret_prompt = art(
        client, h, "prompts", "private-prompt", prompt_spec(), scope, visibility="private"
    )
    pv = publish(client, h, "prompts", secret_prompt["id"])
    agent = art(client, h, "agents", "uses-private", agent_spec(pv["id"], model), scope)
    av = publish(client, h, "agents", agent["id"])
    mine = client.get(f"{A}/agents/{agent['id']}/versions/{av['id']}", headers=h).json()
    assert mine["pins"][0]["key"] == "private-prompt"
    other = as_user(client, world.passwords, "admin2@demo-a.local")
    seen = client.get(f"{A}/agents/{agent['id']}/versions/{av['id']}", headers=other).json()
    assert (
        seen["pins"][0]["key"] == ""
        and seen["pins"][0]["name"] == ""
        and seen["pins"][0]["state"] == "hidden"
    )
    assert "private-prompt" not in str(seen)


# ================================================================== 6. connections
def _role_user(
    client: TestClient, owner: Session, world: World, email: str, key: str, perms: list[str]
) -> None:
    h = login(client, world.passwords, ADMIN)
    role = post(client, f"{A}/roles", h, {"key": key, "name": key, "permissions": perms})["id"]
    uid = new_user(client, h, "demo-a", email, PASSWORD)
    world.passwords[email] = PASSWORD
    h = login(client, world.passwords, ADMIN)
    from tests._support.p2util import tenant_id

    r = client.post(
        f"{A}/role-bindings",
        json={
            "subject_type": "user",
            "subject_id": uid,
            "role_id": role,
            "scope": {"type": "tenant", "id": str(tenant_id(owner, "demo-a"))},
        },
        headers=h,
    )
    assert r.status_code == 201, r.text


def test_connection_ids_are_no_oracle_and_approval_does_not_use_the_approvers_grants(
    client: TestClient, world: World, owner_session: Session, rest_server: RestTestServer
) -> None:
    h = login(client, world.passwords, ADMIN)
    conn = rest_conn(client, h, rest_server, "oracle-api")
    scope = tenant_scope(owner_session)
    _role_user(
        client,
        owner_session,
        world,
        "author@demo-a.local",
        "tool-author",
        ["tool:read", "tool:create", "tool:update", "tool:publish"],
    )
    _role_user(
        client,
        owner_session,
        world,
        "approve-only@demo-a.local",
        "approve-only",
        ["approval:decide", "tool:read"],
    )
    author = as_user(client, world.passwords, "author@demo-a.local")
    t = art(client, author, "tools", "no-conn-read", rest_spec(conn), scope)
    # the author holds neither connection:read nor connection:use: same answer as "no such connection"
    assert vcodes(client, author, "tools", t["id"]) == ["connection_not_found"]

    # the approver lacks connection:use, but the check is the requester's, made at request time
    h = login(client, world.passwords, ADMIN)
    set_tenant_setting(owner_session, "demo-a", "publish_requires_approval", True)
    ok = art(client, h, "tools", "approved-tool", rest_spec(conn), scope)
    req = client.post(f"{A}/tools/{ok['id']}/publish", json={}, headers=h).json()["publish_request"]
    approver = as_user(client, world.passwords, "approve-only@demo-a.local")
    done = client.post(
        f"{A}/publish-requests/{req['id']}/decide", json={"decision": "approve"}, headers=approver
    )
    assert done.status_code == 200, done.text


# ================================================================== 7 + 8. approval edge cases
def test_approval_requires_an_active_artifact_and_private_requests_are_decidable(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    make_user(
        client, owner_session, h, "approver@demo-a.local", "tenant_administrator", world.passwords
    )
    h = login(client, world.passwords, ADMIN)
    set_tenant_setting(owner_session, "demo-a", "publish_requires_approval", True)
    scope = tenant_scope(owner_session)

    p = art(client, h, "prompts", "will-be-disabled", prompt_spec(), scope)
    req = client.post(f"{A}/prompts/{p['id']}/publish", json={}, headers=h).json()[
        "publish_request"
    ]
    cur = client.get(f"{A}/prompts/{p['id']}", headers=h).json()
    assert (
        client.patch(
            f"{A}/prompts/{p['id']}",
            json={"status": "disabled", "row_version": cur["row_version"]},
            headers=h,
        ).status_code
        == 200
    )
    approver = as_user(client, world.passwords, "approver@demo-a.local")
    refused = client.post(
        f"{A}/publish-requests/{req['id']}/decide", json={"decision": "approve"}, headers=approver
    )
    assert refused.status_code == 409 and "active" in refused.json()["detail"]
    state = owner_session.execute(
        text("SELECT state FROM shifde.artifact_versions WHERE artifact_id = :a"), {"a": p["id"]}
    ).scalar_one()
    owner_session.rollback()
    assert state == "draft"

    # a PRIVATE artifact's request is visible to, and decidable by, approvers
    h = login(client, world.passwords, ADMIN)
    q = art(client, h, "prompts", "private-one", prompt_spec(), scope, visibility="private")
    req2 = client.post(f"{A}/prompts/{q['id']}/publish", json={}, headers=h).json()[
        "publish_request"
    ]
    approver = as_user(client, world.passwords, "approver@demo-a.local")
    assert client.get(f"{A}/prompts/{q['id']}", headers=approver).status_code == 404
    listed = client.get(f"{A}/publish-requests?status=pending", headers=approver).json()["items"]
    assert req2["id"] in {i["id"] for i in listed}
    ok = client.post(
        f"{A}/publish-requests/{req2['id']}/decide", json={"decision": "approve"}, headers=approver
    )
    assert ok.status_code == 200, ok.text


# ================================================================== 4. tool runtime


class _FailingConns:
    def __init__(self) -> None:
        self.calls = 0

    @contextmanager
    def open_for_use(self, *_a: Any, **_k: Any) -> Iterator[Any]:
        self.calls += 1
        raise AdapterFailure(ErrorClass.NETWORK, "boom")
        yield  # pragma: no cover


def test_retry_idempotency_keys_and_fresh_keys_per_test_call(
    client: TestClient, world: World, owner_session: Session, rest_server: RestTestServer
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    ro = rest_conn(client, h, rest_server, "flaky-ro")
    rw = rest_conn(client, h, rest_server, "flaky-rw", access="read_write")

    # a transient failure is retried: the second attempt succeeds
    rest_server.flaky_calls = 0
    flaky = rest_spec(
        ro,
        retry={"max_attempts": 2, "backoff_s": 0},
        timeout_s=5,
        binding={"connection_id": ro, "method": "GET", "path_template": "/flaky"},
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
    )
    t = art(client, h, "tools", "flaky", flaky, scope)
    out = run_tool(client, h, t["id"], {})
    assert out["attempts"] == 2 and out["output"]["body"] == {"ok": True, "call": 2}

    # Idempotency-Key: sent for key_supported tools (fresh per test call), absent otherwise
    idem = rest_spec(
        rw,
        "POST",
        "write_idempotent",
        idempotency="key_supported",
        retry={"max_attempts": 2, "backoff_s": 0},
        binding={"connection_id": rw, "method": "POST", "path_template": "/echo"},
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
    )
    w = art(client, h, "tools", "idem-write", idem, scope)
    run_tool(client, h, w["id"], {}, ack=True)
    run_tool(client, h, w["id"], {}, ack=True)
    keys = [r["idempotency_key"] for r in rest_server.requests if r["method"] == "POST"][-2:]
    assert all(keys) and keys[0] != keys[1]
    plain = art(client, h, "tools", "plain-read", rest_spec(ro), scope)
    run_tool(client, h, plain["id"], {"name": "x"})
    assert rest_server.requests[-1]["idempotency_key"] is None
    assert builtin_tool_spec()["idempotency"] == "none" and validate(client, h, w["id"])["valid"]
