"""SP7-2 (design sections 4 and 8, Call S7-B): the catalog, per-project install and per-project
enable/disable.

The catalog equals the design section 4 table; every key it names is a permission row and every
tool an internal function, and every derived artifact key matches `KEY_PATTERN`. A full install
builds 11 distinct `seth-*` tools (shared across the six keys), six prompts and six active agents
with the generated specs; a second install publishes nothing; after a person edits a tool the
reinstall publishes exactly one new version of it; a failure after the first step leaves the row
`installing` with its progress, and a rerun resumes without duplicates. Install is refused,
writing nothing, with assist off or a stale pin (409), for a developer (403), for an unknown,
another client's, an unusable or a not allowed model, for an unknown key (422) and for a disabled
specialist (409). Enable/disable flips the row and the agent; the organization switch makes every
specialist unavailable without rewriting a row.
"""

from __future__ import annotations

import re
from typing import Any  # Any: JSON bodies and rows

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.assist import catalog
from shifde.assist.service import AssistService
from shifde.build.internal_functions import INTERNAL_FUNCTIONS
from shifde.build.schemas import KEY_PATTERN
from shifde.core.errors import Conflict
from shifde.core.ids import uuid7
from tests._support.fullapp import World
from tests._support.p2util import post, tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p4util import edit, publish
from tests._support.p5util import Hdr, user_session
from tests.assist.helpers import (
    PROJECT,
    build_state,
    enable,
    install,
    installed,
    model,
    project_member,
    rows,
    set_settings,
    specialists,
    toggle,
)

DESIGN_TABLE = {
    "research": (("seth.catalog.read", "seth.solutions.read"), ("connection:read",), ()),
    "solution_design": (
        ("seth.catalog.read", "seth.solutions.read", "seth.releases.read"),
        ("connection:read", "packaging:solution:read"),
        (),
    ),
    "build": (
        ("seth.catalog.read", "seth.propose.app_request"),
        ("connection:read", "appgen:request:write"),
        ("appgen_request",),
    ),
    "evaluation": (
        ("seth.releases.read", "seth.evaluations.read", "seth.propose.evaluation_run"),
        ("packaging:solution:read", "evaluation:run:read", "evaluation:run:request"),
        ("evaluation_run",),
    ),
    "cost": (("seth.costs.read", "seth.budgets.read"), ("usage:read", "budget:read"), ()),
    "deployment": (
        ("seth.board.read", "seth.targets.read", "seth.propose.delivery_plan"),
        ("delivery:target:read", "delivery:deployment:read", "delivery:plan:read",
         "delivery:plan:request"),
        ("delivery_plan",),
    ),
}  # fmt: skip
ASSIST_KEYS = {
    "assist:request:read",
    "assist:request:write",
    "assist:proposal:accept",
    "assist:install",
    "assist:admin",
}


# ------------------------------------------------------------------ the catalog
def test_the_catalog_is_the_design_table() -> None:
    assert catalog.CATALOG_VERSION == "assist.catalog/1"
    assert list(catalog.CATALOG) == list(DESIGN_TABLE)
    for key, (tools, perms, kinds) in DESIGN_TABLE.items():
        entry = catalog.CATALOG[key]
        assert (entry.tools, entry.required_permissions, entry.proposal_kinds) == (
            tools, perms, kinds,
        )  # fmt: skip
        for name in catalog.VARIABLES:
            assert "{{" + name + "}}" in entry.instructions


def test_catalog_tools_and_keys_are_declared_and_valid() -> None:
    tools = {t for e in catalog.CATALOG.values() for t in e.tools}
    assert len(tools) == 11 and tools <= set(INTERNAL_FUNCTIONS)
    keys = [catalog.tool_artifact_key(t) for t in tools] + [
        catalog.artifact_key(k) for k in catalog.CATALOG
    ]
    assert all(re.match(KEY_PATTERN, k) for k in keys), keys
    assert catalog.tool_artifact_key("seth.propose.app_request") == "seth-propose-app_request"
    for t in tools:
        spec = catalog.tool_spec(t)
        decl = INTERNAL_FUNCTIONS[t]
        assert spec["binding"] == {"function_key": t}
        assert (spec["input_schema"], spec["output_schema"], spec["side_effect_class"]) == (
            decl.input_schema, decl.output_schema, decl.side_effect_class,
        )  # fmt: skip
    model_id, prompt = uuid7(), uuid7()
    agent = catalog.agent_spec("cost", model_id, prompt, [uuid7()], 1.5)
    assert agent["approval_rules"] == {"require_approval_for": []}
    assert agent["limits"] == {"max_iterations": 8, "max_tool_calls": 12, "max_tokens": 100000,
                               "deadline_s": 120, "max_cost": 1.5}  # fmt: skip
    prompt_spec = catalog.prompt_spec("cost")
    assert prompt_spec["sections"]["system"] == catalog.CATALOG["cost"].instructions
    assert [v["name"] for v in prompt_spec["variables"]] == list(catalog.VARIABLES)


def test_every_catalog_and_assist_permission_is_a_permission_row(
    owner_session: Session,
) -> None:
    wanted = {p for e in catalog.CATALOG.values() for p in e.required_permissions} | ASSIST_KEYS
    found = {
        r[0]
        for r in rows(owner_session, "SELECT key FROM shifde.permissions WHERE key = ANY(:k)",
                      k=sorted(wanted))
    }  # fmt: skip
    assert found == wanted, sorted(wanted - found)


# ------------------------------------------------------------------ install
def _artifacts(owner: Session, kind: str) -> list[tuple[Any, ...]]:
    """(key, status, versions, latest published spec) of demo-a's `kind` artifacts in `sample`."""
    return rows(
        owner,
        "SELECT a.key, a.status, (SELECT count(*) FROM shifde.artifact_versions v "
        "WHERE v.artifact_id = a.id), (SELECT v.spec FROM shifde.artifact_versions v "
        "WHERE v.id = a.latest_published_version_id) FROM shifde.artifacts a "
        "JOIN shifde.projects p ON p.id = a.project_id WHERE p.slug = 'sample' "
        "AND a.tenant_id = :t AND a.kind = :k ORDER BY a.key",
        t=tenant_id(owner, "demo-a"),
        k=kind,
    )


def _admin(client: TestClient, world: World) -> tuple[Hdr, str]:
    admin = user_session(client, world.passwords, ADMIN)
    enable(admin)
    return admin, model(admin)


def test_a_full_install_builds_the_catalog_once(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin, model_id = _admin(client, world)
    out = installed(admin, model_id)
    assert [s["key"] for s in out] == list(catalog.CATALOG)
    assert all(s["status"] == "enabled" and s["available"] for s in out)
    tools = _artifacts(owner_session, "tools")
    assert len(tools) == 11 and all(t[0].startswith("seth-") for t in tools)
    for key, status, versions, spec in tools:
        internal = spec["binding"]["function_key"]
        assert key == catalog.tool_artifact_key(internal)
        assert (status, versions, spec) == ("active", 1, catalog.tool_spec(internal))
    prompts = _artifacts(owner_session, "prompts")
    assert [p[0] for p in prompts] == sorted(catalog.artifact_key(k) for k in catalog.CATALOG)
    agents = _artifacts(owner_session, "agents")
    assert len(agents) == 6 and {a[1] for a in agents} == {"active"}
    progress = rows(owner_session, "SELECT key, install_progress FROM shifde.assist_specialists")
    assert {k: len(p) for k, p in progress} == {
        k: len(e.tools) + 2 for k, e in catalog.CATALOG.items()
    }

    before = build_state(owner_session)
    again = installed(admin, model_id)
    assert [s["agent_version_id"] for s in again] == [s["agent_version_id"] for s in out]
    assert build_state(owner_session) == before  # nothing new published


def test_a_reinstall_republishes_an_edited_tool_once(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin, model_id = _admin(client, world)
    installed(admin, model_id, keys=["cost"])
    [tool_id] = rows(owner_session,
                     "SELECT id::text FROM shifde.artifacts WHERE key = 'seth-costs-read'")[0]  # fmt: skip
    changed = {**catalog.tool_spec("seth.costs.read"), "timeout_s": 7}
    edit(admin.client, admin, "tools", tool_id, changed)
    publish(admin.client, admin, "tools", tool_id, "a person's edit")
    [(_, _, versions, _)] = [
        t for t in _artifacts(owner_session, "tools") if t[0] == "seth-costs-read"
    ]
    assert versions == 2
    installed(admin, model_id, keys=["cost"])
    tools = {t[0]: t for t in _artifacts(owner_session, "tools")}
    assert tools["seth-costs-read"][2] == 3
    assert tools["seth-costs-read"][3] == catalog.tool_spec("seth.costs.read")
    assert tools["seth-budgets-read"][2] == 1


def test_an_interrupted_install_resumes_without_duplicates(
    client: TestClient, world: World, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, model_id = _admin(client, world)
    publish_real = AssistService._publish
    calls: list[str] = []

    def failing(self: AssistService, kind: str, artifact_id: Any) -> Any:  # Any: ids
        calls.append(kind)
        if len(calls) > 1:
            raise Conflict("simulated failure")
        return publish_real(self, kind, artifact_id)

    monkeypatch.setattr(AssistService, "_publish", failing)
    r = install(admin, model_id, keys=["research"])
    assert r.status_code == 409, r.text
    [(status, progress)] = rows(
        owner_session, "SELECT status, install_progress FROM shifde.assist_specialists"
    )
    assert status == "installing"
    assert [p["step"] for p in progress] == ["tool:seth-catalog-read"]
    listed = specialists(admin).json()
    assert [(s["status"], s["available"]) for s in listed] == [("installing", False)]

    monkeypatch.setattr(AssistService, "_publish", publish_real)
    [done] = installed(admin, model_id, keys=["research"])
    assert done["status"] == "enabled"
    counts = rows(
        owner_session,
        "SELECT kind, count(*) FROM shifde.artifacts GROUP BY kind ORDER BY kind",
    )
    assert dict(counts) == {"agents": 1, "prompts": 1, "tools": 2}
    [(progress,)] = rows(owner_session, "SELECT install_progress FROM shifde.assist_specialists")
    assert [p["step"] for p in progress] == [
        "tool:seth-catalog-read", "tool:seth-solutions-read", "prompt:assist-research",
        "agent:assist-research",
    ]  # fmt: skip
    assert len(rows(owner_session, "SELECT 1 FROM shifde.assist_specialists")) == 1


# ------------------------------------------------------------------ refusals
def _refused(
    admin: Hdr, owner: Session, body_model: str, status: int, code: str | None, **kw: Any
) -> None:
    before = build_state(owner)
    r = install(admin, body_model, **kw)
    assert r.status_code == status, r.text
    if code is not None:
        assert r.json()["code"] == code, r.text
    assert build_state(owner) == before


def test_install_needs_assist_enabled_on_the_current_pin(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    model_id = model(admin)
    _refused(admin, owner_session, model_id, 409, "assist_not_enabled")
    set_settings(owner_session, "demo-a", assist_enabled=True, catalog_version="assist.catalog/0")
    _refused(admin, owner_session, model_id, 409, "assist_not_enabled")
    enable(admin)
    set_settings(owner_session, "demo-a", assist_enabled=False)
    _refused(admin, owner_session, model_id, 409, "assist_not_enabled")


def test_a_developer_cannot_install(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin, model_id = _admin(client, world)
    p1 = admin.client.get(f"{A}{PROJECT}", headers=admin).json()["id"]
    dev = project_member(client, world, "installer@demo-a.local", p1)
    _refused(dev, owner_session, model_id, 403, None)
    r = install(dev, model_id)
    assert "assist:install" in r.json()["detail"] and "agent:activate" in r.json()["detail"]


def test_install_refuses_a_model_it_cannot_use(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin, model_id = _admin(client, world)
    admin_b = user_session(client, world.passwords, ADMIN_B)
    theirs = model(admin_b, B)
    unknown = install(admin, str(uuid7()))
    assert unknown.status_code == 404, unknown.text
    _refused(admin, owner_session, theirs, 404, None)
    assert install(admin, theirs).json()["detail"] == unknown.json()["detail"]
    deprecated = model(admin, lifecycle="deprecated")
    _refused(admin, owner_session, deprecated, 422, "model_unavailable")
    tenant = str(tenant_id(owner_session, "demo-a"))
    owner_session.rollback()
    other = model(admin)
    put = admin.client.put(
        f"{A}/model-allowlists",
        params={"scope_type": "tenant", "scope_id": tenant},
        json={"enabled": True, "model_ids": [other]},
        headers=admin,
    )
    assert put.status_code == 200, put.text
    _refused(admin, owner_session, model_id, 422, "model_not_allowed")


def test_install_refuses_an_unknown_or_repeated_key(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin, model_id = _admin(client, world)
    _refused(admin, owner_session, model_id, 422, "specialist_unknown", keys=["oracle"])
    _refused(admin, owner_session, model_id, 422, "specialist_unknown", keys=["cost", "cost"])


# ------------------------------------------------------------------ enable and disable
def _agent_status(owner: Session, agent_id: str) -> str:
    [(status,)] = rows(owner, "SELECT status FROM shifde.artifacts WHERE id = :a", a=agent_id)
    return str(status)


def test_enable_and_disable_flip_the_row_and_the_agent(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin, model_id = _admin(client, world)
    [cost] = installed(admin, model_id, keys=["cost"])
    r = toggle(admin, "cost", "disable")
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["available"]) == ("disabled", False)
    assert _agent_status(owner_session, cost["agent_id"]) == "disabled"
    _refused(admin, owner_session, model_id, 409, "specialist_disabled", keys=["cost"])
    r = toggle(admin, "cost", "enable")
    assert (r.json()["status"], r.json()["available"]) == ("enabled", True)
    assert _agent_status(owner_session, cost["agent_id"]) == "active"
    assert toggle(admin, "oracle", "enable").json()["code"] == "specialist_unknown"
    missing = toggle(admin, "research", "enable")
    assert (missing.status_code, missing.json()["detail"]) == (404, "specialist not found")

    # the organization switch: every specialist unavailable, no row rewritten
    before = build_state(owner_session)
    post(admin.client, f"{A}/assist/disable", admin, None, 200)
    assert build_state(owner_session) == before
    [listed] = specialists(admin).json()
    assert (listed["status"], listed["available"]) == ("enabled", False)
    catalog_out = admin.client.get(f"{A}/assist/catalog", headers=admin).json()
    assert catalog_out["assist_enabled"] is False
    [entry] = [e for e in catalog_out["entries"] if e["key"] == "cost"]
    assert [(i["status"], i["available"]) for i in entry["installs"]] == [("enabled", False)]
    assert all("instructions" not in e for e in catalog_out["entries"])
    enable(admin)
    assert specialists(admin).json()[0]["available"] is True


def test_toggling_an_installing_specialist_is_refused(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin, model_id = _admin(client, world)
    installed(admin, model_id, keys=["cost"])
    owner_session.execute(text("UPDATE shifde.assist_specialists SET status = 'installing'"))
    owner_session.commit()
    r = toggle(admin, "cost", "disable")
    assert (r.status_code, r.json()["code"]) == (409, "specialist_installing")
