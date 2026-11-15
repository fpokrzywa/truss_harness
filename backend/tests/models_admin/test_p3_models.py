"""SETH 3ba0e06 `tests/test_p3_models.py`: the tests that drive invoke, `/models/effective`,
allowlists, routes and limits (carry_forward row 20, DECISIONS B-1719, B-1736), assertions
unchanged. SETH's seeded `demo-a` / `demo-b` and their admins are the tenancy route world's clients
A and B and their owners; `main` / `sample` are A's first workspace and its project. SETH's
`login` becomes `as_user` (one session per client: signing in as another person replaces it), its
`POST /secrets` and `/secrets/{id}/rotate` (admin routes, not ported) the secrets service.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import OpenAIFakeServer
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.secrets import service as secrets_service
from tests.connections.routes import activate, as_user, connection, org, post, secret
from tests.models_admin.conftest import start_of_window
from tests.models_admin.routes import fake_model_stack, invoke_body
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, project_slug, ws_slug

OAI_KEY = "sk-oai-test-key-0001"


@pytest.fixture
def oai_server() -> Iterator[OpenAIFakeServer]:
    with OpenAIFakeServer(api_key=OAI_KEY) as server:
        yield server


def test_register_a_model_and_invoke_it_through_the_fake_provider(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A, config={"script": [{"match": "ping", "reply": "pong"}]})
    provider = client.get(f"{A}/model-providers/{ids['provider']}", headers=h).json()
    assert set(provider) >= {
        "id",
        "name",
        "adapter_key",
        "connection_id",
        "status",
        "health_status",
        "row_version",
    }
    health = post(client, f"{A}/model-providers/{ids['provider']}/health-check", h, {}, 200)
    assert health["status"] == "healthy" and set(health) == {
        "status",
        "latency_ms",
        "checked_at",
        "detail",
    }
    assert (
        client.get(f"{A}/model-providers/{ids['provider']}", headers=h).json()["health_status"]
        == "healthy"
    )
    disc = post(client, f"{A}/model-providers/{ids['provider']}/discover-models", h, {}, 200)
    assert [m["model_key"] for m in disc["items"]] == ["fake-chat"]

    out = post(client, f"{A}/models/{ids['model']}/invoke", h, invoke_body("ping me"), 200)
    assert out["output"] == "pong" and set(out["usage"]) == {"input_tokens", "output_tokens"}
    assert out["latency_ms"] >= 0 and out["provider_request_id"].startswith("fake-")
    again = post(client, f"{A}/models/{ids['model']}/invoke", h, invoke_body("ping me"), 200)
    assert again["output"] == out["output"]  # deterministic

    # audit carries metadata only (capture mode is `metadata` outside development)
    rows = (
        owner_session.execute(
            text(
                "SELECT details FROM shifde.audit_events WHERE action = 'model.invoked' "
                "AND tenant_id = :t ORDER BY id"
            ),
            {"t": world.a.tenant},
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2 and "ping me" not in json.dumps(rows) and "pong" not in json.dumps(rows)
    assert rows[0]["usage"]["input"] == out["usage"]["input_tokens"] and "messages" not in rows[0]
    # content capture `full` opts the prompt/completion in
    owner_session.execute(
        text('UPDATE shifde.tenants SET settings = \'{"capture_content": "full"}\' WHERE id = :t'),
        {"t": world.a.tenant},
    )
    owner_session.commit()
    post(client, f"{A}/models/{ids['model']}/invoke", h, invoke_body("ping me"), 200)
    full = owner_session.execute(
        text(
            "SELECT details FROM shifde.audit_events WHERE action = 'model.invoked' "
            "AND tenant_id = :t ORDER BY id DESC LIMIT 1"
        ),
        {"t": world.a.tenant},
    ).scalar_one()
    assert full["messages"][0]["content"] == "ping me" and full["output"] == "pong"

    listing = client.get(f"{A}/models", headers=h).json()
    assert [m["id"] for m in listing["items"]] == [ids["model"]]
    assert listing["items"][0]["pricing_ref"] is None  # reserved for P8


def test_model_permissions_and_cross_tenant_isolation(
    client: TestClient, world: World, people: People
) -> None:
    A, B = org(world.a.slug), org(world.b.slug)
    h = as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A)
    hv = as_user(client, people.viewer_a)
    assert client.get(f"{A}/models/{ids['model']}", headers=hv).status_code == 200  # model:read
    assert (
        client.post(f"{A}/models/{ids['model']}/invoke", json=invoke_body(), headers=hv).status_code
        == 403
    )
    assert client.post(f"{A}/model-providers", headers=hv, json={}).status_code in (403, 422)
    hb = as_user(client, people.owner_b)
    for path in (
        f"{B}/models/{ids['model']}",
        f"{B}/model-providers/{ids['provider']}",
        f"{B}/connections/{ids['connection']}",
    ):
        assert client.get(path, headers=hb).status_code == 404, path
    assert (
        client.post(f"{B}/models/{ids['model']}/invoke", json=invoke_body(), headers=hb).status_code
        == 404
    )
    # a provider cannot reference another tenant's connection
    r = client.post(
        f"{B}/model-providers",
        headers=hb,
        json={"name": "steal", "adapter_key": "fake", "connection_id": ids["connection"]},
    )
    assert r.status_code == 404


def test_model_lifecycle_transitions_b7(client: TestClient, world: World, people: People) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A, lifecycle="preview")
    url = f"{A}/models/{ids['model']}"

    def move(to: str) -> Any:
        row = client.get(url, headers=h).json()
        return client.patch(
            url, json={"lifecycle": to, "row_version": row["row_version"]}, headers=h
        )

    assert move("active").status_code == 200
    assert move("preview").status_code == 409  # active -> preview is not allowed
    assert move("deprecated").status_code == 200
    # deprecated models are no longer invocable or effective
    assert client.post(f"{url}/invoke", json=invoke_body(), headers=h).status_code == 409
    assert client.get(f"{A}/models/effective", headers=h).json()["items"] == []
    assert move("active").status_code == 200  # deprecated -> active
    assert client.post(f"{url}/invoke", json=invoke_body(), headers=h).status_code == 200
    assert move("retired").status_code == 200
    assert move("active").status_code == 409  # retired is terminal
    row = client.get(url, headers=h).json()
    assert (
        client.patch(
            url, json={"display_name": "x", "row_version": row["row_version"]}, headers=h
        ).status_code
        == 409
    )
    r = client.post(f"{url}/invoke", json=invoke_body(), headers=h)
    assert r.status_code == 409 and r.json()["code"] == "model_unavailable"
    dup = client.post(
        f"{A}/models",
        headers=h,
        json={
            "provider_id": ids["provider"],
            "model_key": "fake-chat",
            "display_name": "d",
            "context_window": 1,
            "max_output_tokens": 1,
        },
    )
    assert dup.status_code == 409


def test_unhealthy_or_disabled_providers_block_invocation(
    client: TestClient,
    world: World,
    people: People,
    oai_server: OpenAIFakeServer,
    app_db: Database,
    settings: Settings,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A)
    off = client.patch(
        f"{A}/model-providers/{ids['provider']}", json={"status": "disabled"}, headers=h
    )
    assert off.status_code == 200
    r = client.post(f"{A}/models/{ids['model']}/invoke", json=invoke_body(), headers=h)
    assert r.status_code == 409 and r.json()["code"] == "model_unavailable"
    assert client.get(f"{A}/models/effective", headers=h).json()["items"] == []
    client.patch(f"{A}/model-providers/{ids['provider']}", json={"status": "active"}, headers=h)
    assert (
        client.post(f"{A}/models/{ids['model']}/invoke", json=invoke_body(), headers=h).status_code
        == 200
    )

    # an OpenAI-compatible provider whose key was rotated away: health-check -> unavailable -> 409
    sec = secret(app_db, settings, world.a.tenant, OAI_KEY)
    conn = connection(
        client,
        h,
        {
            "name": "oai",
            "connection_type": "model_api",
            "adapter_key": "openai_compatible",
            "config": {"base_url": f"{oai_server.base_url}/v1"},
            "auth_method": "bearer",
            "secret_id": sec,
        },
        A,
    )
    activate(client, h, conn["id"], A)
    prov = post(
        client,
        f"{A}/model-providers",
        h,
        {"name": "oai-prov", "adapter_key": "openai_compatible", "connection_id": conn["id"]},
    )
    good = post(client, f"{A}/model-providers/{prov['id']}/health-check", h, {}, 200)
    assert good["status"] == "healthy"
    model = post(
        client,
        f"{A}/models",
        h,
        {
            "provider_id": prov["id"],
            "model_key": "fake-gpt",
            "display_name": "GPT",
            "capabilities": ["chat"],
            "context_window": 4096,
            "max_output_tokens": 512,
            "lifecycle": "active",
        },
    )
    out = post(client, f"{A}/models/{model['id']}/invoke", h, invoke_body("hi there", 8), 200)
    assert out["output"] == "echo: hi there" and out["provider_request_id"] == "req-test-1"
    with app_session(app_db, world.a.tenant) as s:
        meta = secrets_service.get_secret(s, UUID(sec), tenant_id=world.a.tenant)
        assert meta is not None
        secrets_service.rotate_secret(
            s, settings.keyring, meta, value="rotated-key-9999", expected_version=None
        )
        s.commit()
    bad = post(client, f"{A}/model-providers/{prov['id']}/health-check", h, {}, 200)
    assert bad["status"] == "unavailable"
    blocked = client.post(f"{A}/models/{model['id']}/invoke", json=invoke_body(), headers=h)
    assert blocked.status_code == 409 and blocked.json()["code"] == "model_unavailable"


# ================================================================== allowlists / routes
def test_allowlists_intersect_down_the_scope_chain(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, B = org(world.a.slug), org(world.b.slug)
    h = as_user(client, people.owner_a)
    one = fake_model_stack(client, h, A, model_key="m-one", conn_name="c1")
    two = fake_model_stack(client, h, A, model_key="m-two", conn_name="c2")
    m1, m2 = one["model"], two["model"]
    tid = str(world.a.tenant)
    ws = {"id": str(world.a.ws)}
    pj = {"id": str(world.a.project)}
    main, sample = ws_slug(world.a), project_slug(world.a)
    url = f"{A}/model-allowlists"

    assert client.get(url, params={"scope_type": "tenant", "scope_id": tid}, headers=h).json() == {
        "enabled": False,
        "model_ids": [],
        "scope_type": "tenant",
        "scope_id": tid,
    }

    def effective(**q: str) -> list[str]:
        r = client.get(f"{A}/models/effective", params=q, headers=h)
        return sorted(m["id"] for m in r.json()["items"])

    assert effective() == sorted([m1, m2])  # no allowlist: everything usable

    def put(scope_type: str, scope_id: str, enabled: bool, ids: list[str]) -> Any:
        return client.put(
            url,
            params={"scope_type": scope_type, "scope_id": scope_id},
            headers=h,
            json={"enabled": enabled, "model_ids": ids},
        )

    assert put("tenant", tid, True, [m1, m2]).status_code == 200
    assert put("workspace", ws["id"], True, [m2]).status_code == 200
    assert effective() == sorted([m1, m2])
    assert effective(workspace=main) == [m2]  # intersection: tenant ∩ workspace
    assert put("project", pj["id"], True, [m1]).status_code == 200
    assert effective(workspace=main, project=sample) == []  # ∩ project allows only m1 (not in ws)
    # enforcement on invoke follows the same chain
    assert client.post(f"{A}/models/{m1}/invoke", json=invoke_body(), headers=h).status_code == 200
    denied = client.post(
        f"{A}/models/{m1}/invoke", params={"workspace": main}, json=invoke_body(), headers=h
    )
    assert denied.status_code == 403 and denied.json()["code"] == "model_not_allowed"
    allowed = client.post(
        f"{A}/models/{m2}/invoke", params={"workspace": main}, json=invoke_body(), headers=h
    )
    assert allowed.status_code == 200
    # a disabled allowlist restricts nothing
    assert put("project", pj["id"], False, [m1]).status_code == 200
    assert effective(workspace=main, project=sample) == [m2]
    got = client.get(
        url, params={"scope_type": "workspace", "scope_id": ws["id"]}, headers=h
    ).json()
    assert got["enabled"] is True and got["model_ids"] == [m2]
    # validation + authorization
    assert put("tenant", tid, True, ["00000000-0000-0000-0000-000000000001"]).status_code == 422
    assert put("tenant", ws["id"], True, [m1]).status_code == 404  # wrong scope id for the type
    hv = as_user(client, people.viewer_a)
    assert (
        client.put(
            url,
            params={"scope_type": "tenant", "scope_id": tid},
            headers=hv,
            json={"enabled": False, "model_ids": []},
        ).status_code
        == 403
    )
    hb = as_user(client, people.owner_b)
    assert (
        client.put(
            f"{B}/model-allowlists",
            params={"scope_type": "tenant", "scope_id": tid},
            headers=hb,
            json={"enabled": False, "model_ids": []},
        ).status_code
        == 404
    )
    audit = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.audit_events WHERE action = 'model.allowlist.updated' "
            "AND tenant_id = :t"
        ),
        {"t": world.a.tenant},
    ).scalar_one()
    assert audit == 4
    h = as_user(client, people.owner_a)
    assert (
        client.get(f"{A}/models/effective", params={"workspace": "nope"}, headers=h).status_code
        == 404
    )


def test_model_routes_are_validated_against_scope_and_lifecycle(
    client: TestClient, world: World, people: People
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    m1 = fake_model_stack(client, h, A, model_key="r1", conn_name="c1")["model"]
    m2 = fake_model_stack(client, h, A, model_key="r2", conn_name="c2")["model"]
    m3 = fake_model_stack(client, h, A, model_key="r3", conn_name="c3", lifecycle="deprecated")[
        "model"
    ]
    tid = str(world.a.tenant)
    body = {
        "name": "default",
        "scope": {"type": "tenant", "id": tid},
        "primary_model_id": m1,
        "fallback_model_ids": [m2],
        "conditions": {"task": "chat"},
    }
    route = post(client, f"{A}/model-routes", h, body)
    assert route["scope"] == {"type": "tenant", "id": tid} and route["fallback_model_ids"] == [m2]
    assert client.get(f"{A}/model-routes", headers=h).json()["items"][0]["id"] == route["id"]
    for bad in (
        {"name": "b1", "primary_model_id": m3},  # deprecated
        {"name": "b2", "fallback_model_ids": [m1]},  # duplicates the primary
        {"name": "b3", "primary_model_id": "00000000-0000-0000-0000-000000000009"},
    ):
        assert (
            client.post(f"{A}/model-routes", json={**body, **bad}, headers=h).status_code == 422
        ), bad
    # not allowed at the scope -> rejected, even for updates
    client.put(
        f"{A}/model-allowlists",
        params={"scope_type": "tenant", "scope_id": tid},
        headers=h,
        json={"enabled": True, "model_ids": [m1]},
    )
    assert (
        client.post(f"{A}/model-routes", json={**body, "name": "b4"}, headers=h).status_code == 422
    )
    upd = client.patch(
        f"{A}/model-routes/{route['id']}",
        headers=h,
        json={"fallback_model_ids": [m2], "row_version": route["row_version"]},
    )
    assert upd.status_code == 422
    ok = client.patch(
        f"{A}/model-routes/{route['id']}",
        headers=h,
        json={"fallback_model_ids": [], "conditions": {}, "row_version": route["row_version"]},
    )
    assert ok.status_code == 200 and ok.json()["fallback_model_ids"] == []
    hv = as_user(client, people.viewer_a)
    assert client.post(f"{A}/model-routes", json=body, headers=hv).status_code == 403


# ================================================================== limits
def test_requests_per_minute_limit_is_enforced(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A, rate_limits={"rpm": 2})
    url = f"{A}/models/{ids['model']}/invoke"
    start_of_window(owner_session)  # the three calls below must share one per-minute window
    assert [client.post(url, json=invoke_body(), headers=h).status_code for _ in range(2)] == [
        200,
        200,
    ]
    limited = client.post(url, json=invoke_body(), headers=h)
    assert limited.status_code == 429 and limited.json()["code"] == "model_rate_limited"
    other = fake_model_stack(client, h, A, model_key="free", conn_name="free-conn")
    assert (
        client.post(
            f"{A}/models/{other['model']}/invoke", json=invoke_body(), headers=h
        ).status_code
        == 200
    )


def test_tokens_per_minute_limit_is_enforced(
    client: TestClient, world: World, people: People
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A, rate_limits={"tpm": 40})
    url = f"{A}/models/{ids['model']}/invoke"
    words = "word " * 25
    assert (
        client.post(url, json=invoke_body(words), headers=h).status_code == 200
    )  # ~51 tokens used
    r = client.post(url, json=invoke_body(words), headers=h)
    assert r.status_code == 429 and r.json()["code"] == "model_rate_limited"


def test_concurrency_limit_is_enforced_across_requests(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(
        client, h, A, rate_limits={"max_concurrency": 1}, config={"latency_ms": 5000}
    )
    url = f"{A}/models/{ids['model']}/invoke"
    results: list[int] = []

    def call() -> None:
        results.append(client.post(url, json=invoke_body(), headers=h).status_code)

    threads = [threading.Thread(target=call) for _ in range(2)]
    threads[0].start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # wait until the first call holds its lease
        owner_session.rollback()
        if owner_session.execute(
            text("SELECT count(*) FROM shifde.concurrency_leases")
        ).scalar_one():
            break
        time.sleep(0.05)
    threads[1].start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == [200, 429]
    assert client.post(url, json=invoke_body(), headers=h).status_code == 200  # slot was released
