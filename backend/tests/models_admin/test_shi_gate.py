"""The SHI call-time gate (plan Review Focus 5, design 5.3, DECISIONS B-1732..B-1734): a client
whose switch is off that tries to call a shared SHI model is refused before any network call, and
the refusal is audited in that client; a client whose switch is on reaches SHI's endpoint, which is
installation configuration. Shared models are priced from global rate cards only (review 1 item 2)
and are never visible through a 2b path while the switch is off (review 2 item 1)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from shifde import ledger
from shifde.adapters.testservers import OpenAIFakeServer
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.ledger import pricing
from shifde.models_admin import repository
from tests.connections.routes import as_user, org
from tests.ledger.util import add_rate
from tests.models_admin.conftest import (
    Pair,
    make_shi,
    model_values,
    provider_values,
)
from tests.models_admin.routes import (
    PLATFORM,
    fake_model_stack,
    invoke_body,
    models_client,
    shared_model,
    switch,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People

SHI_KEY = "sk-shi-installation-key-0001"
_VARYING = {"instance", "request_id"}


@dataclass(frozen=True)
class Shi:
    client: TestClient
    server: OpenAIFakeServer
    model: str


@pytest.fixture
def shi(settings: Settings, world: World, people: People, app_db: Database) -> Iterator[Shi]:
    """An installation whose SHI provider is an OpenAI-compatible endpoint (a local test server
    that records every chat call and accepts only the installation key); A's switch on, B's off."""
    with OpenAIFakeServer(api_key=SHI_KEY) as server:
        configured = settings.model_copy(
            update={
                "shi_provider_config": {"base_url": f"{server.base_url}/v1"},
                "shi_provider_api_key": SecretStr(SHI_KEY),
            }
        )
        with models_client(configured) as client:
            h = as_user(client, people.platform)
            r = client.put(
                f"{PLATFORM}/shi-provider",
                headers=h,
                json={"adapter_key": "openai_compatible", "status": "active"},
            )
            assert r.status_code == 200, r.text
            model = shared_model(app_db, model_key="fake-gpt")
            switch(client, people.platform, world.a.slug, True)
            yield Shi(client, server, model)


def _same(r: Any, missing: Any, status: int = 404) -> None:
    assert r.status_code == missing.status_code == status, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


def _audit(owner_db: Database, tenant: UUID) -> list[tuple[str, dict[str, Any]]]:
    with owner_db.session() as o:
        return [
            (str(r[0]), dict(r[1]))
            for r in o.execute(
                text(
                    "SELECT outcome, details FROM shifde.audit_events WHERE tenant_id = :t "
                    "AND action = 'model.invoked' ORDER BY id"
                ),
                {"t": tenant},
            )
        ]


def _usage(owner_db: Database, tenant: UUID) -> int:
    with owner_db.session() as o:
        return int(
            o.execute(
                text("SELECT count(*) FROM shifde.usage_events WHERE tenant_id = :t"),
                {"t": tenant},
            ).scalar_one()
        )


def test_a_client_whose_switch_is_off_is_refused_before_any_network_call(
    shi: Shi, world: World, people: People, owner_db: Database
) -> None:
    client, server = shi.client, shi.server
    A, B = org(world.a.slug), org(world.b.slug)
    h = as_user(client, people.owner_a)
    out = client.post(f"{A}/models/{shi.model}/invoke", headers=h, json=invoke_body("hi shi"))
    assert out.status_code == 200, out.text
    assert out.json()["output"] == "echo: hi shi"  # SHI's endpoint, with the installation key
    assert len(server.bodies) == 1
    assert isinstance(server.bodies[0].get("max_tokens"), int)  # Item 9: a ceiling is sent
    assert _usage(owner_db, world.a.tenant) == 1  # Item 11: the usage row is written

    # B (switch off): answered like an unknown model, nothing sent, the refusal audited in B
    hb = as_user(client, people.owner_b)
    missing = client.post(f"{B}/models/{uuid7()}/invoke", headers=hb, json=invoke_body())
    refused = client.post(f"{B}/models/{shi.model}/invoke", headers=hb, json=invoke_body())
    _same(refused, missing)
    assert len(server.bodies) == 1
    [(outcome, details)] = _audit(owner_db, world.b.tenant)
    assert outcome == "denied" and details["reason"] == "shi_provider_off"
    assert details["target"] == "provider:SHI"  # the label names the provider, not the model
    assert {k: details[k] for k in ("workspace_id", "project_id", "run_id")} == {
        "workspace_id": None,
        "project_id": None,
        "run_id": None,
    }  # Item 15: the context keys are always present
    assert _usage(owner_db, world.b.tenant) == 0

    # A's switch turned off: A is refused the same way, before any call
    switch(client, people.platform, world.a.slug, False)
    h = as_user(client, people.owner_a)
    missing = client.post(f"{A}/models/{uuid7()}/invoke", headers=h, json=invoke_body())
    refused = client.post(f"{A}/models/{shi.model}/invoke", headers=h, json=invoke_body())
    _same(refused, missing)
    assert len(server.bodies) == 1
    outcomes = [(o, d.get("reason")) for o, d in _audit(owner_db, world.a.tenant)]
    assert outcomes == [("success", None), ("denied", "shi_provider_off")]
    assert _usage(owner_db, world.a.tenant) == 1


def test_shared_models_follow_the_switch_on_every_2b_path(
    shi: Shi, world: World, people: People, app_db: Database
) -> None:
    client = shi.client
    for side, on in ((world.a, True), (world.b, False)):
        base = org(side.slug)
        h = as_user(client, people.owner_a if on else people.owner_b)
        own = fake_model_stack(client, h, base, conn_name=f"c-{side.slug}")["model"]
        effective = client.get(f"{base}/models/effective", headers=h).json()["items"]
        assert (shi.model in {m["id"] for m in effective}) is on
        # allowlists and routes name own models only (B-1733): the shared model is an unknown id
        scope = {"scope_type": "tenant", "scope_id": str(side.tenant)}
        missing = client.put(
            f"{base}/model-allowlists",
            headers=h,
            params=scope,
            json={"enabled": True, "model_ids": [str(uuid7())]},
        )
        named = client.put(
            f"{base}/model-allowlists",
            headers=h,
            params=scope,
            json={"enabled": True, "model_ids": [shi.model]},
        )
        _same(named, missing, 422)
        route = {"name": "r", "scope": {"type": "tenant", "id": str(side.tenant)}}
        missing = client.post(
            f"{base}/model-routes",
            headers=h,
            json={**route, "primary_model_id": own, "fallback_model_ids": [str(uuid7())]},
        )
        named = client.post(
            f"{base}/model-routes",
            headers=h,
            json={**route, "primary_model_id": own, "fallback_model_ids": [shi.model]},
        )
        _same(named, missing, 422)
        with app_session(app_db, side.tenant) as s:  # the repository honours the switch
            ids = [UUID(shi.model)]
            assert repository.get_models(s, ids, tenant_id=side.tenant, shared=False) == []
            seen = repository.get_models(s, ids, tenant_id=side.tenant, shared=on)
            assert [r["id"] for r in seen] == (ids if on else [])


def test_the_shi_endpoint_is_never_called_unconfigured(
    client: TestClient, world: World, people: People, app_db: Database
) -> None:
    """No `SHIFDE_SHI_PROVIDER_CONFIG`: a client whose switch is on gets model_unavailable."""
    h = as_user(client, people.platform)
    r = client.put(
        f"{PLATFORM}/shi-provider", headers=h, json={"adapter_key": "fake", "status": "active"}
    )
    assert r.status_code == 200, r.text
    model = shared_model(app_db)
    switch(client, people.platform, world.a.slug, True)
    h = as_user(client, people.owner_a)
    out = client.post(f"{org(world.a.slug)}/models/{model}/invoke", headers=h, json=invoke_body())
    assert out.status_code == 409 and out.json()["code"] == "model_unavailable", out.text


def test_shared_models_are_priced_from_global_rate_cards_only(
    pair: Pair, app_db: Database, owner_db: Database
) -> None:
    """Review 1 item 2: a client card naming the shared model's keys does not price it (a client
    could otherwise price SHI's calls at zero for itself); it still prices the client's own model
    with the same keys."""
    a = pair.a.tenant
    shared = make_shi(app_db)  # adapter `fake`, model key `shi-model`
    with app_session(app_db, a) as s:
        provider = provider_values(a, pair.a.connection)
        repository.insert_provider(s, provider)
        own = model_values(a, provider["id"], model_key="shi-model")
        repository.insert_model(s, own)
        s.commit()
    with owner_db.session() as o:
        global_version = add_rate(o, None, "fake", "shi-model", input_rate="5")
        client_version = add_rate(o, a, "fake", "shi-model", input_rate="0")
        o.commit()
    usage = pricing.Usage(input_tokens=1000, output_tokens=0, calls=1)
    with app_session(app_db, a) as s:
        costed, _, _ = ledger.price_model_usage(s, a, shared, usage)
        assert costed.rate_card_version_id == global_version
        costed, _, _ = ledger.price_model_usage(s, a, own["id"], usage)
        assert costed.rate_card_version_id == client_version
        cost = ledger.estimate.call_cost(s, a, shared, 1000, 1)
        assert cost is not None and cost.amount > 0
    with app_session(app_db, pair.b.tenant) as s:  # B has no card of its own: the global one
        costed, _, _ = ledger.price_model_usage(s, pair.b.tenant, shared, usage)
        assert costed.rate_card_version_id == global_version
