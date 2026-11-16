"""P8 governance policies (contract section 6 item 7): configurable approval rules and cost
thresholds on top of the P5/P6/P7 approval paths, the policy decision log, retention settings,
export policies, model policies; every change and decision audited."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A, fake_model_stack, invoke_body
from tests._support.p4util import tenant_scope
from tests._support.p5util import (
    tool_call_reply,
)

ECHO = {"match": "weather", "reply": tool_call_reply("echo", {"text": "tool-output"})}
FINAL = {"match": "Tool results", "reply": "done"}


def _rows(owner: Session, sql: str, **p: Any) -> list[Any]:
    return list(owner.execute(text(sql), p).all())


def _policy(
    client: TestClient, h: dict[str, str], kind: str, definition: dict[str, Any], **extra: Any
) -> Any:
    r = client.post(
        f"{A}/governance/policies",
        json={"kind": kind, "name": f"test {kind}", "definition": definition, **extra},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _approvals(client: TestClient, h: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = client.get(f"{A}/approvals?status=pending", headers=h).json()[
        "items"
    ]
    return out


# ------------------------------------------------------------------ approval rules


# ------------------------------------------------------------------ administration


# ------------------------------------------------------------------ retention


# ------------------------------------------------------------------ export + model policies


def test_a_model_policy_writes_through_to_the_p3_allowlist_and_blocks_other_models(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    one = fake_model_stack(client, h, model_key="allowed-m", conn_name="fake-allowed")
    two = fake_model_stack(client, h, model_key="blocked-m", conn_name="fake-blocked")
    tid = tenant_scope(owner_session)["id"]
    pol = _policy(
        client,
        h,
        "model_policy",
        {"allowed_model_ids": [one["model"]]},
        scope=tenant_scope(owner_session),
    )
    assert pol["enforcement"] == "enforced"
    allow = client.get(f"{A}/model-allowlists?scope_type=tenant&scope_id={tid}", headers=h).json()
    assert allow["enabled"] is True and allow["model_ids"] == [one["model"]]
    ok = client.post(f"{A}/models/{one['model']}/invoke", json=invoke_body("hi"), headers=h)
    blocked = client.post(f"{A}/models/{two['model']}/invoke", json=invoke_body("hi"), headers=h)
    assert ok.status_code == 200 and blocked.status_code == 403
    assert blocked.json()["code"] == "model_not_allowed"
    # the P3 allowlist stays the source of truth: the policy shows what is in force
    client.put(
        f"{A}/model-allowlists?scope_type=tenant&scope_id={tid}",
        json={"enabled": True, "model_ids": [one["model"], two["model"]]},
        headers=h,
    )
    shown = client.get(f"{A}/governance/policies/{pol['id']}", headers=h).json()
    assert sorted(shown["definition"]["allowed_model_ids"]) == sorted([one["model"], two["model"]])
    # direct invocations are metered too
    kinds = _rows(owner_session, "SELECT event_type, is_test FROM shifde.usage_events")
    assert ("model_call", False) in kinds
