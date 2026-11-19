"""Governance policies, retention settings and the data-export policy through the API, moved from
SETH 3ba0e06 `test_p8_policies.py` (policy validation and audit, policy administration and
tenant scope, retention settings, the data-export policy, and, with runtime part 5 (B-2134), the
side-effect-class test); assertions unchanged.

Setup changes only: SETH `login` / `user_session` become `as_user` on the tenancy route-test
people, `limited_user` the connections helper, `tenant_scope` the helper in
`tests.governance.routes`, and the old policy-decision row is written as the schema owner for
the client of the tenancy world. Not moved: the approval-rule and cost-threshold tests and the
decision-log test (they need runs, workers and approvals: runtime), the model-policy test (needs
the models routes and an invoke: app).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database
from tests.connections.routes import as_user, limited_user, org
from tests.governance.routes import CURRENCY, tenant_scope
from tests.tenancy.conftest import World
from tests.tenancy.routes import People


def _rows(owner: Session, sql: str, **p: Any) -> list[Any]:
    return list(owner.execute(text(sql), p).all())


def _policy(
    client: TestClient,
    h: dict[str, str],
    base: str,
    kind: str,
    definition: dict[str, Any],
    **extra: Any,
) -> Any:
    r = client.post(
        f"{base}/governance/policies",
        json={"kind": kind, "name": f"test {kind}", "definition": definition, **extra},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_policy_definitions_are_validated_per_kind_and_every_change_is_audited(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a = org(world.a.slug)
    h = as_user(client, people.owner_a)
    bad: list[tuple[str, dict[str, Any]]] = [
        ("cost_threshold", {"threshold_amount": -1, "currency": CURRENCY}),
        ("cost_threshold", {"threshold_amount": 1, "currency": "usd"}),
        ("cost_threshold", {"threshold_amount": 1, "currency": CURRENCY, "action": "nuke"}),
        ("lifecycle", {"artifact_kinds": ["bogus"], "deprecate_unused_after_days": 30}),
        ("lifecycle", {"artifact_kinds": ["prompts"], "deprecate_unused_after_days": 0}),
        ("approval_rule", {"when": {}}),
        ("approval_rule", {"when": {"estimated_cost_gt": 5}}),  # needs a currency
        ("approval_rule", {"when": {"side_effect_classes": ["x"]}}),
        ("approval_rule", {"when": {"side_effect_classes": ["destructive"]},
                           "approver_permission": "no:such"}),
        ("approval_rule", {"when": {"side_effect_classes": ["destructive"]},
                           "approver_permission": "platform:admin"}),
        # F-10 (#17): the data_export and target_environments conditions
        ("approval_rule", {"when": {"data_export": "yes"}}),
        ("approval_rule", {"when": {"data_export": 1}}),
        ("approval_rule", {"when": {"target_environments": []}}),
        ("approval_rule", {"when": {"target_environments": "prod"}}),
        ("approval_rule", {"when": {"target_environments": [""]}}),
        ("approval_rule", {"when": {"target_environments": [1]}}),
        ("approval_rule", {"when": {"target_environments": ["e" * 65]}}),
        ("approval_rule", {"when": {"target_environments": [f"e{i}" for i in range(51)]}}),
        ("approval_rule", {"when": {"data_export": True, "environment": "prod"}}),
        ("data_export", {}),
        ("data_export", {"max_rows": 0}),
        ("model_policy", {"allowed_model_ids": ["not-a-uuid"]}),
    ]  # fmt: skip
    for kind, definition in bad:
        r = client.post(
            f"{a}/governance/policies", json={"kind": kind, "definition": definition}, headers=h
        )
        assert r.status_code == 422, (kind, definition, r.text)
    life = _policy(
        client,
        h,
        a,
        "lifecycle",
        {"artifact_kinds": ["prompts", "tools"], "deprecate_unused_after_days": 90},
    )
    assert life["enforcement"] == "not_enforced"  # stored and reported only
    thr = _policy(
        client, h, a, "cost_threshold",
        {"threshold_amount": 12.5, "currency": CURRENCY, "action": "alert"},
        scope=tenant_scope(world.a),
    )  # fmt: skip
    assert thr["version"] == 1 and thr["scope"]["type"] == "tenant"
    upd = client.patch(
        f"{a}/governance/policies/{thr['id']}",
        json={"definition": {"threshold_amount": 20, "currency": CURRENCY, "action": "require_approval"},
              "row_version": thr["row_version"]},
        headers=h,
    )  # fmt: skip
    assert upd.status_code == 200 and upd.json()["version"] == 2
    stale = client.patch(
        f"{a}/governance/policies/{thr['id']}",
        json={"status": "disabled", "row_version": thr["row_version"]},
        headers=h,
    )
    assert stale.status_code == 409
    listed = client.get(f"{a}/governance/policies?kind=cost_threshold", headers=h).json()["items"]
    assert [p["id"] for p in listed] == [thr["id"]]
    actions = [r[0] for r in _rows(owner_session, "SELECT action FROM shifde.audit_events")]
    assert actions.count("governance.policy.created") == 2
    assert "governance.policy.updated" in actions


def test_policy_admin_requires_governance_permissions_and_is_tenant_scoped(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, b = org(world.a.slug), org(world.b.slug)
    h = as_user(client, people.owner_a)
    pol = _policy(client, h, a, "data_export", {"allow_export": True})
    vh = as_user(client, people.viewer_a)
    assert client.get(f"{a}/governance/policies", headers=vh).status_code == 403
    assert client.get(f"{a}/governance/overview", headers=vh).status_code == 403
    assert client.get(f"{a}/governance/decisions", headers=vh).status_code == 403
    assert client.get(f"{a}/governance/retention", headers=vh).status_code == 403
    assert (
        client.post(
            f"{a}/governance/policies",
            json={"kind": "data_export", "definition": {"allow_export": False}},
            headers=vh,
        ).status_code
        == 403
    )
    email, _ = limited_user(owner_db, world.a.tenant, ["governance:read"])
    rh = as_user(client, email)
    assert client.get(f"{a}/governance/policies", headers=rh).status_code == 200
    assert (
        client.patch(
            f"{a}/governance/policies/{pol['id']}", json={"status": "disabled"}, headers=rh
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"{a}/governance/retention",
            json={
                "items": [{"data_class": "usage_events", "retain_days": 60, "legal_hold": False}]
            },
            headers=rh,
        ).status_code
        == 403
    )
    bh = as_user(client, people.owner_b)
    assert client.get(f"{b}/governance/policies", headers=bh).json()["items"] == []
    assert client.get(f"{b}/governance/policies/{pol['id']}", headers=bh).status_code == 404
    assert (
        client.patch(
            f"{b}/governance/policies/{pol['id']}", json={"status": "disabled"}, headers=bh
        ).status_code
        == 404
    )


def test_retention_settings_have_floors_holds_and_say_the_purge_is_not_implemented(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a = org(world.a.slug)
    h = as_user(client, people.owner_a)
    got = client.get(f"{a}/governance/retention", headers=h).json()
    assert got["enforcement"] == "not_implemented" and "not implemented" in got["note"]
    by = {i["data_class"]: i for i in got["items"]}
    assert by["audit_events"]["floor_days"] == 90 and by["audit_events"]["is_default"] is True
    too_low = client.put(
        f"{a}/governance/retention",
        json={"items": [{"data_class": "audit_events", "retain_days": 30, "legal_hold": False}]},
        headers=h,
    )
    assert too_low.status_code == 422 and "platform floor" in too_low.text
    dup = client.put(
        f"{a}/governance/retention",
        json={"items": [{"data_class": "usage_events", "retain_days": 60}] * 2},
        headers=h,
    )
    assert dup.status_code == 422
    put = client.put(
        f"{a}/governance/retention",
        json={
            "items": [
                {"data_class": "audit_events", "retain_days": 100, "legal_hold": False},
                {"data_class": "usage_events", "retain_days": 31, "legal_hold": True},
            ]
        },
        headers=h,
    )
    assert put.status_code == 200
    by = {i["data_class"]: i for i in put.json()["items"]}
    assert by["audit_events"]["retain_days"] == 100 and by["usage_events"]["legal_hold"] is True
    assert by["policy_decisions"]["is_default"] is True
    # preview: counts only, nothing deleted; a legal hold counts zero
    before = _rows(owner_session, "SELECT count(*) FROM shifde.audit_events")[0][0]
    owner_session.execute(
        text(
            "INSERT INTO shifde.policy_decisions (id, tenant_id, at, principal_type, action, "
            "decision) VALUES (gen_random_uuid(), :t, now() - interval '400 days', 'user', "
            "'old.action', 'allow')"
        ),
        {"t": world.a.tenant},
    )
    owner_session.commit()
    prev = client.post(f"{a}/governance/retention/preview", headers=h)
    assert prev.status_code == 200 and prev.json()["enforcement"] == "not_implemented"
    counts = {i["data_class"]: i for i in prev.json()["items"]}
    assert counts["policy_decisions"]["count"] == 1  # 365-day default, 400-day-old row
    assert counts["usage_events"]["on_legal_hold"] is True and counts["usage_events"]["count"] == 0
    what_if = client.post(
        f"{a}/governance/retention/preview",
        json={
            "items": [{"data_class": "policy_decisions", "retain_days": 500, "legal_hold": False}]
        },
        headers=h,
    ).json()
    assert {i["data_class"]: i["count"] for i in what_if["items"]}["policy_decisions"] == 0
    assert (
        _rows(
            owner_session,
            "SELECT count(*) FROM shifde.policy_decisions WHERE action = 'old.action'",
        )[0][0]
        == 1
    )
    assert _rows(owner_session, "SELECT count(*) FROM shifde.audit_events")[0][0] >= before
    changed = _rows(
        owner_session,
        "SELECT details FROM shifde.audit_events WHERE action = 'governance.retention.updated'",
    )
    assert changed and changed[0][0]["purge"] == "not_implemented"


def test_a_data_export_policy_can_forbid_exports_and_exports_need_data_export(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a = org(world.a.slug)
    h = as_user(client, people.owner_a)
    assert client.get(f"{a}/usage/export?format=csv", headers=h).status_code == 200
    assert client.get(f"{a}/usage/export?format=xml", headers=h).status_code == 422
    vh = as_user(client, people.viewer_a)  # no data:export
    assert client.get(f"{a}/usage/export?format=csv", headers=vh).status_code == 403
    assert client.get(f"{a}/audit-events/export", headers=vh).status_code == 403
    h = as_user(client, people.owner_a)  # one cookie jar: signing in as the viewer replaced it
    pol = _policy(client, h, a, "data_export", {"allow_export": False})
    assert client.get(f"{a}/usage/export?format=csv", headers=h).status_code == 403
    assert client.get(f"{a}/audit-events/export", headers=h).status_code == 403
    client.patch(f"{a}/governance/policies/{pol['id']}", json={"status": "disabled"}, headers=h)
    assert client.get(f"{a}/usage/export?format=csv", headers=h).status_code == 200
    exported = _rows(
        owner_session, "SELECT action FROM shifde.audit_events WHERE action LIKE '%.exported'"
    )
    assert ("governance.usage.exported",) in exported


def test_governance_side_effect_classes_match_the_build_module() -> None:
    from shifde.build.executors import SIDE_EFFECT_CLASSES
    from shifde.governance.validation import SIDE_EFFECT_CLASSES as GOV_CLASSES

    assert set(SIDE_EFFECT_CLASSES) == set(GOV_CLASSES)
