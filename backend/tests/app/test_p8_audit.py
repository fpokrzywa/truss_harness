"""SETH 3ba0e06 `tests/test_p8_audit.py`: the audit explorer and export through the full app
(categories, a workspace-scoped auditor, the formula-neutralising CSV export that is itself
audited, base permissions). All four tests; assertions unchanged; imports name `tests._support.*`.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.audit import service as audit
from shifde.core.context import RequestContext
from tests._support.fullapp import World
from tests._support.p2util import login, post, tenant_id
from tests._support.p3util import ADMIN, VIEWER, A
from tests._support.p4util import tenant_scope
from tests._support.p5util import BASE_PERMS, limited_user, user_session
from tests._support.p8util import budget


def _event(owner: Session, action: str, workspace_id: UUID | None, **details: Any) -> None:
    audit.record(
        owner,
        RequestContext(request_id="t-audit"),
        action=action,
        tenant_id=tenant_id(owner, "demo-a"),
        workspace_id=workspace_id,
        target_type="x",
        target_id="1",
        details=details,
    )
    owner.commit()


def _workspace(client: TestClient, h: dict[str, str], slug: str) -> str:
    return str(post(client, f"{A}/workspaces", h, {"slug": slug, "name": slug.title()})["id"])


def test_categories_group_actions_and_unknown_categories_are_refused(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    budget(client, h, tenant_scope(owner_session), amount=5)  # governance.budget.created
    for action in ("run.completed", "approval.decided", "ontology.queried", "vendor.created"):
        _event(owner_session, action, None)

    def actions(category: str) -> set[str]:
        r = client.get(f"{A}/audit-events?category={category}&limit=200", headers=h)
        assert r.status_code == 200, r.text
        return {e["action"] for e in r.json()["items"]}

    assert actions("execution") == {"run.completed", "approval.decided"}
    assert actions("governance") == {"governance.budget.created"}
    assert "ontology.queried" in actions("data") and "run.completed" not in actions("data")
    assert "vendor.created" in actions("admin")
    assert any(a.startswith("identity.") for a in actions("security"))
    assert {"run.completed", "governance.budget.created"} <= {
        e["action"] for e in client.get(f"{A}/audit-events?limit=200", headers=h).json()["items"]
    }  # no category = everything
    assert client.get(f"{A}/audit-events?category=nonsense", headers=h).status_code == 422
    assert client.get(f"{A}/audit-events/verify", headers=h).json()["ok"] is True


def test_a_workspace_auditor_sees_and_exports_only_its_own_workspace(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    main = str(
        owner_session.execute(
            text("SELECT id FROM shifde.workspaces WHERE slug = 'main' AND tenant_id = :t"),
            {"t": tenant_id(owner_session, "demo-a")},
        ).scalar_one()
    )
    other = _workspace(client, h, "other")
    _event(owner_session, "run.completed", UUID(main), where="main")
    _event(owner_session, "run.failed", UUID(other), where="other")
    _event(owner_session, "tenancy.org.updated", None, where="tenant-level")
    # a custom role holding audit:read, bound only to workspace `main`
    role = post(
        client,
        f"{A}/roles",
        h,
        {
            "key": "ws-auditor",
            "name": "WS auditor",
            "permissions": ["workspace:read", "audit:read"],
        },
    )
    uid, _ = limited_user(
        client, owner_session, h, world.passwords, "audit-ws@demo-a.local", ["data:export"]
    )
    h = login(client, world.passwords, ADMIN)
    post(
        client,
        f"{A}/role-bindings",
        h,
        {
            "subject_type": "user",
            "subject_id": uid,
            "role_id": role["id"],
            "scope": {"type": "workspace", "id": main},
        },
    )
    ah = user_session(client, world.passwords, "audit-ws@demo-a.local")
    seen = ah.client.get(f"{A}/audit-events?limit=200", headers=ah).json()["items"]
    assert seen and {e["workspace_id"] for e in seen} == {main}
    assert {e["action"] for e in seen} >= {"run.completed"}
    assert not {e["action"] for e in seen} & {"run.failed", "tenancy.org.updated"}
    # the hash chain verification is tenant-level and stays so
    assert ah.client.get(f"{A}/audit-events/verify", headers=ah).status_code == 403
    exported = ah.client.get(f"{A}/audit-events/export?format=json", headers=ah)
    assert exported.status_code == 200, exported.text
    rows = [json.loads(line) for line in exported.text.splitlines()]
    assert rows and {r["workspace_id"] for r in rows} == {main}
    assert {r["action"] for r in rows} >= {"run.completed"} and "run.failed" not in str(rows)
    # a viewer-style principal with no audit:read anywhere gets the standard 403
    vh = user_session(client, world.passwords, VIEWER)
    assert vh.client.get(f"{A}/audit-events", headers=vh).status_code == 403
    # the tenant administrator still sees everything
    everything = client.get(
        f"{A}/audit-events?limit=200", headers=login(client, world.passwords, ADMIN)
    )
    assert {"run.failed", "tenancy.org.updated"} <= {
        e["action"] for e in everything.json()["items"]
    }


def test_audit_export_is_filtered_neutralises_formulas_and_is_itself_audited(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    _event(owner_session, "run.completed", None, note="=HYPERLINK(1)")
    _event(owner_session, "vendor.created", None)
    r = client.get(f"{A}/audit-events/export?format=csv&category=execution", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="audit.csv"' in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert {row["action"] for row in rows} == {"run.completed"}
    assert all(not c.startswith("=") for row in rows for c in row.values())  # CSV injection
    assert list(rows[0]) == [
        "occurred_at", "actor", "action", "outcome", "target", "workspace_id", "project_id",
        "chain_seq", "hash", "request_id", "details",
    ]  # fmt: skip
    assert client.get(f"{A}/audit-events/export?format=xml", headers=h).status_code == 422
    assert client.get(f"{A}/audit-events/export?category=bogus", headers=h).status_code == 422
    exported = [
        json.loads(r[0]) if isinstance(r[0], str) else r[0]
        for r in owner_session.execute(
            text("SELECT details FROM shifde.audit_events WHERE action = 'audit.exported'")
        )
    ]
    assert exported and exported[0]["rows"] == 1 and exported[0]["category"] == "execution"
    assert client.get(f"{A}/audit-events/verify", headers=h).json()["ok"] is True
    data_events = client.get(f"{A}/audit-events?category=data", headers=h).json()["items"]
    assert "audit.exported" in {e["action"] for e in data_events}


def test_base_permissions_do_not_include_audit_or_export(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    limited_user(client, owner_session, h, world.passwords, "plain2@demo-a.local", BASE_PERMS)
    ph = user_session(client, world.passwords, "plain2@demo-a.local")
    assert ph.client.get(f"{A}/audit-events/export", headers=ph).status_code == 403
    assert ph.client.get(f"{A}/usage/export", headers=ph).status_code == 403
