"""P4 review round 2: unstorable JSON, artifact lock order / latest pointer, normalisation,
approval transaction, no-name messages for approvers, path templates, scoped approvers."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.authz import service as authz
from shifde.build.service import ArtifactService
from shifde.core import db
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.tenancy.scope import TenantScope
from tests._support.fullapp import World
from tests._support.p2util import login, new_user, post, tenant_id
from tests._support.p3util import ADMIN, A, fake_model_stack
from tests._support.p4util import (
    PASSWORD,
    agent_spec,
    art,
    as_user,
    edit,
    prompt_spec,
    publish,
    set_tenant_setting,
    tenant_scope,
)
from tests.app.test_build_review1 import vcodes
from tests.app.test_build_tools import rest_conn, rest_spec


def raw(client: TestClient, method: str, url: str, h: dict[str, str], body: str) -> Any:
    return client.request(
        method, url, content=body, headers={**h, "Content-Type": "application/json"}
    )


# ================================================================== 1. unstorable JSON
@pytest.mark.parametrize(
    "fragment",
    ["NaN", "Infinity", "-Infinity", "1e400", '"a\\u0000b"', '"a\\ud800b"', '"\\udc00"'],
)
def test_json_that_postgres_cannot_store_is_a_422_everywhere(
    client: TestClient, world: World, owner_session: Session, fragment: str
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    p = art(client, h, "prompts", "storable", prompt_spec(), scope)
    t = art(client, h, "tools", "storable-tool", {}, scope)
    spec = '{"sections": {"system": "s"}, "x": FRAG}'.replace("FRAG", fragment)
    sc = '{"type": "tenant", "id": "ID"}'.replace("ID", scope["id"])
    cases = [
        ("PUT", f"{A}/prompts/{p['id']}/draft", '{"spec": SPEC, "row_version": 1}'),
        (
            "POST",
            f"{A}/prompts",
            '{"key": "k1", "name": "n", "scope": SC, "visibility": "scope", "spec": SPEC}',
        ),
        (
            "POST",
            f"{A}/prompts/{p['id']}/render",
            '{"version_id": "draft", "variables": {"message": FRAG}}',
        ),
        ("POST", f"{A}/tools/{t['id']}/test", '{"version_id": "draft", "args": {"a": FRAG}}'),
        ("POST", f"{A}/prompts/{p['id']}/publish", '{"change_note": FRAG}'),
        (
            "POST",
            f"{A}/agents/{p['id']}/test",
            '{"version_id": "draft", "variables": {"v": FRAG}, "input": ""}',
        ),
    ]
    cases = [
        (m, u, b.replace("SPEC", spec).replace("SC", sc).replace("FRAG", fragment))
        for m, u, b in cases
    ]
    for method, url, body in cases:
        r = raw(client, method, url, h, body)
        # a number fragment is a type error for change_note (422) - never a 500
        assert r.status_code in (404, 422), (url, r.status_code, r.text[:200])
    # keys and the search term as well
    key_spec = '{"spec": {"k\\u0000ey": 1}, "row_version": 1}'
    assert raw(client, "PUT", f"{A}/prompts/{p['id']}/draft", h, key_spec).status_code == 422
    assert client.get(f"{A}/prompts?q=a%00b", headers=h).status_code == 422
    # the draft is untouched
    got = client.get(f"{A}/prompts/{p['id']}/draft", headers=h).json()
    assert got["row_version"] == 1


def test_nul_in_a_decision_comment_is_rejected(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    r = raw(
        client,
        "POST",
        f"{A}/publish-requests/00000000-0000-0000-0000-000000000000/decide",
        h,
        '{"decision": "approve", "comment": "a\\u0000b"}',
    )
    assert r.status_code == 422


# ================================================================== 2. latest pointer under races
def test_concurrent_publish_and_deprecate_keep_latest_pointing_at_a_published_version(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    p = art(client, h, "prompts", "pointer", prompt_spec(), tenant_scope(owner_session))
    v1 = publish(client, h, "prompts", p["id"])
    edit(client, h, "prompts", p["id"], prompt_spec(sections={"system": "v2"}), 0)
    # hold the artifact row: both operations pass authorization, then queue on the row lock
    owner_session.execute(
        text("SELECT id FROM shifde.artifacts WHERE id = :a FOR UPDATE"), {"a": p["id"]}
    )
    out: dict[str, Any] = {}

    def do_publish() -> None:
        out["pub"] = client.post(f"{A}/prompts/{p['id']}/publish", json={}, headers=h)

    def do_deprecate() -> None:
        out["dep"] = client.post(
            f"{A}/prompts/{p['id']}/versions/{v1['id']}/state",
            json={"state": "deprecated"},
            headers=h,
        )

    threads = [threading.Thread(target=do_publish), threading.Thread(target=do_deprecate)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    assert all(t.is_alive() for t in threads)
    owner_session.commit()
    for t in threads:
        t.join(30)
    assert out["pub"].status_code == 200 and out["dep"].status_code == 200
    detail = client.get(f"{A}/prompts/{p['id']}", headers=h).json()
    assert detail["latest_published_version"] == {"id": out["pub"].json()["id"], "version_no": 2}
    states = {
        r[0]: r[1]
        for r in owner_session.execute(
            text("SELECT version_no, state FROM shifde.artifact_versions WHERE artifact_id = :a"),
            {"a": p["id"]},
        )
    }
    owner_session.rollback()
    assert states == {1: "deprecated", 2: "published"}


# ================================================================== 3. normalisation is load-bearing
def _service(app_db: Database, settings: Settings, owner: Session, uid: Any) -> ArtifactService:
    tid = tenant_id(owner, "demo-a")
    session = app_db.session()
    db.set_tenant(session, tid)
    db.set_principal(session, uid)
    principal = Principal(user_id=uid, email="t@x", display_name="t")
    scope = TenantScope(
        session=session,
        ctx=RequestContext(request_id="t", principal=principal),
        principal=principal,
        tenant_id=tid,
        tenant_slug="demo-a",
        tenant_name="A",
        tenant_status="active",
        pdp=authz.new_pdp(session, principal, tid),
    )
    return ArtifactService(
        scope, IntegrationRuntime(settings=settings, registry=AdapterRegistry.load())
    )


# ================================================================== 5. one transaction at approval


# ================================================================== scoped approvers + no names
def _scoped_user(
    client: TestClient,
    owner: Session,
    world: World,
    email: str,
    key: str,
    perms: list[str],
    scope_type: str = "tenant",
    scope_id: str | None = None,
) -> None:
    h = login(client, world.passwords, ADMIN)
    role = post(client, f"{A}/roles", h, {"key": key, "name": key, "permissions": perms})["id"]
    uid = new_user(client, h, "demo-a", email, PASSWORD)
    world.passwords[email] = PASSWORD
    h = login(client, world.passwords, ADMIN)
    r = client.post(
        f"{A}/role-bindings",
        json={
            "subject_type": "user",
            "subject_id": uid,
            "role_id": role,
            "scope": {"type": scope_type, "id": scope_id or str(tenant_id(owner, "demo-a"))},
        },
        headers=h,
    )
    assert r.status_code == 201, r.text


def test_an_approver_of_another_workspace_neither_sees_nor_decides_the_request(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    w1 = post(client, f"{A}/workspaces", h, {"slug": "w1", "name": "W1"})
    w2 = post(client, f"{A}/workspaces", h, {"slug": "w2", "name": "W2"})
    _scoped_user(
        client,
        owner_session,
        world,
        "w2approver@demo-a.local",
        "ws-approver",
        ["approval:decide", "prompt:read"],
        "workspace",
        w2["id"],
    )
    h = login(client, world.passwords, ADMIN)
    set_tenant_setting(owner_session, "demo-a", "publish_requires_approval", True)
    p = art(client, h, "prompts", "in-w1", prompt_spec(), {"type": "workspace", "id": w1["id"]})
    req = client.post(f"{A}/prompts/{p['id']}/publish", json={}, headers=h).json()[
        "publish_request"
    ]
    other = as_user(client, world.passwords, "w2approver@demo-a.local")
    assert client.get(f"{A}/publish-requests", headers=other).json()["items"] == []
    r = client.post(
        f"{A}/publish-requests/{req['id']}/decide", json={"decision": "approve"}, headers=other
    )
    assert r.status_code == 404
    still = owner_session.execute(text("SELECT status FROM shifde.publish_requests")).scalar_one()
    owner_session.rollback()
    assert still == "pending"


def test_approval_revalidation_does_not_name_artifacts_the_approver_cannot_read(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    model = fake_model_stack(client, h)["model"]
    _scoped_user(
        client,
        owner_session,
        world,
        "blind@demo-a.local",
        "blind-approver",
        ["approval:decide", "agent:read"],
    )
    h = login(client, world.passwords, ADMIN)
    prompt = art(client, h, "prompts", "very-secret-prompt-name", prompt_spec(), scope)
    pv = publish(client, h, "prompts", prompt["id"])
    set_tenant_setting(owner_session, "demo-a", "publish_requires_approval", True)
    agent = art(client, h, "agents", "blind-agent", agent_spec(pv["id"], model), scope)
    req = client.post(f"{A}/agents/{agent['id']}/publish", json={}, headers=h).json()[
        "publish_request"
    ]
    for to in ("deprecated", "retired"):
        r = client.post(
            f"{A}/prompts/{prompt['id']}/versions/{pv['id']}/state", json={"state": to}, headers=h
        )
        assert r.status_code == 200
    blind = as_user(client, world.passwords, "blind@demo-a.local")
    assert client.get(f"{A}/prompts/{prompt['id']}", headers=blind).status_code == 404
    refused = client.post(
        f"{A}/publish-requests/{req['id']}/decide", json={"decision": "approve"}, headers=blind
    )
    assert refused.status_code == 422
    assert "reference_retired" in refused.text and "very-secret-prompt-name" not in refused.text


# ================================================================== 7. path templates
def test_path_templates_reject_dot_segments_and_backslashes(
    client: TestClient, world: World, owner_session: Session, rest_server: Any
) -> None:
    h = login(client, world.passwords, ADMIN)
    ro = rest_conn(client, h, rest_server, "dots-api")
    t = art(client, h, "tools", "dots", rest_spec(ro), tenant_scope(owner_session))
    rv = 1
    for bad in ("/a/./b", "/a\\b", "/%2e%2e/x", "/%2E/x", "/a/../b"):
        spec = rest_spec(ro)
        spec["binding"]["path_template"] = bad
        edit(client, h, "tools", t["id"], spec, rv)
        rv += 1
        assert vcodes(client, h, "tools", t["id"]) == ["invalid_path"], bad
    ok = rest_spec(ro)
    ok["binding"]["path_template"] = "/a.b/c-d"
    edit(client, h, "tools", t["id"], ok, rv)
    assert vcodes(client, h, "tools", t["id"]) == []
