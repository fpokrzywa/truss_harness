"""SP7-1 (design sections 3 and 4): the `seth.*` internal function declarations in build.

`INTERNAL_FUNCTIONS` declares exactly the eleven keys of design section 4 with their classes and
valid draft 2020-12 schemas, disjoint from the builtin functions; the `builtin.function` binding
schema lists both sets. A `builtin.function` tool bound to a `seth.*` key is created and published
like a builtin one (and `side_effect_understated` / `unknown_function` still apply), but it runs
only inside a run: `run_builtin` and the tool test panel refuse it (422) and run nothing. B's
administrator asking for A's `seth.*` tool, and a member bound only on A's project P1 asking for
the one of A's P2, get the unknown-id 404. The route tests have a database of their own (the
full-app harness of `tests/app`).
"""

from __future__ import annotations

from typing import Any

import httpx
import jsonschema
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde import worker_main
from shifde.build import tool_runtime
from shifde.build.executors import BUILTIN_FUNCTIONS, SIDE_EFFECT_CLASSES, binding_schema
from shifde.build.internal_functions import INTERNAL_FUNCTIONS
from shifde.core.errors import ValidationFailed
from shifde.core.ids import uuid7
from shifde.runtime.internal_tools import InternalToolRegistry
from tests._support.fullapp import World
from tests._support.p2util import new_user, post
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p4util import art, builtin_tool_spec, codes, publish
from tests._support.p5util import Hdr, user_session
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
    "test_database",
    "world",
]

KEY = "seth.board.read"
REFUSAL = "seth.* functions run only inside an agent run"
CLASSES = {
    "seth.catalog.read": "read",
    "seth.solutions.read": "read",
    "seth.releases.read": "read",
    "seth.evaluations.read": "read",
    "seth.costs.read": "read",
    "seth.budgets.read": "read",
    "seth.board.read": "read",
    "seth.targets.read": "read",
    "seth.propose.app_request": "write_idempotent",
    "seth.propose.evaluation_run": "write_idempotent",
    "seth.propose.delivery_plan": "write_idempotent",
}
_VARYING = {"instance", "request_id"}


# ------------------------------------------------------------------ declarations
def test_the_eleven_declarations_and_their_schemas() -> None:
    assert {k: d.side_effect_class for k, d in INTERNAL_FUNCTIONS.items()} == CLASSES
    assert not set(INTERNAL_FUNCTIONS) & set(BUILTIN_FUNCTIONS)
    for key, decl in INTERNAL_FUNCTIONS.items():
        assert decl.key == key and decl.description
        assert decl.side_effect_class in SIDE_EFFECT_CLASSES
        for schema in (decl.input_schema, decl.output_schema):
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            jsonschema.Draft202012Validator.check_schema(schema)
        assert decl.input_schema["additionalProperties"] is False
        out = decl.output_schema
        assert out["type"] == "object"
        assert out["properties"]["truncated"] == {"type": "boolean"}
        assert out["properties"]["error"] == {"type": "string"}
        jsonschema.Draft202012Validator(out).validate({"error": "not_found"})
    with pytest.raises(TypeError):
        INTERNAL_FUNCTIONS["seth.x.read"] = INTERNAL_FUNCTIONS[KEY]  # type: ignore[index]


def test_the_input_schemas_follow_the_design() -> None:
    def ok(key: str, args: dict[str, Any]) -> bool:
        return jsonschema.Draft202012Validator(
            INTERNAL_FUNCTIONS[key].input_schema, format_checker=jsonschema.FormatChecker()
        ).is_valid(args)

    uid = str(uuid7())
    assert ok("seth.board.read", {}) and not ok("seth.board.read", {"x": 1})
    assert ok("seth.costs.read", {"days": 90}) and not ok("seth.costs.read", {"days": 91})
    assert not ok("seth.evaluations.read", {}) and ok("seth.evaluations.read", {"release_id": uid})
    assert not ok("seth.catalog.read", {"connection_id": "not-a-uuid"})
    app = {"connection_id": uid, "tables": ["t"], "brief": "b", "model_id": uid}
    assert ok("seth.propose.app_request", app)
    assert not ok("seth.propose.app_request", {**app, "tables": []})
    plan = {"target_id": uid, "release_id": uid, "application_key": "app-1", "display_name": "A"}
    assert ok("seth.propose.delivery_plan", plan)
    assert not ok("seth.propose.delivery_plan", {**plan, "application_key": "App"})


def test_the_binding_schema_lists_builtin_and_internal_keys() -> None:
    enum = binding_schema("builtin.function")["properties"]["function_key"]["enum"]
    assert enum == sorted(BUILTIN_FUNCTIONS.keys() | INTERNAL_FUNCTIONS.keys())


def test_run_builtin_refuses_an_internal_key() -> None:
    with pytest.raises(ValidationFailed, match=r"seth\.\* functions run only inside an agent run"):
        tool_runtime.run_builtin(KEY, {})


# ------------------------------------------------------------------ routes
def _seth_tool(key: str = KEY, side: str | None = None) -> dict[str, Any]:
    decl = INTERNAL_FUNCTIONS.get(key) or INTERNAL_FUNCTIONS[KEY]
    return builtin_tool_spec(
        binding={"function_key": key},
        input_schema=decl.input_schema,
        output_schema=decl.output_schema,
        side_effect_class=side or decl.side_effect_class,
    )


def _project(h: Hdr, slug: str) -> str:
    c = h.client
    if slug != "sample":
        post(c, f"{A}/workspaces/main/projects", h, {"slug": slug, "name": slug})
    return str(c.get(f"{A}/workspaces/main/projects/{slug}", headers=h).json()["id"])


def _same(r: httpx.Response, ref: httpx.Response) -> None:
    """Refused exactly like an id that does not exist: no existence hint."""
    assert r.status_code == ref.status_code == 404, (r.status_code, r.text, ref.text)
    got, want = r.json(), ref.json()
    assert {k: v for k, v in got.items() if k not in _VARYING} == {
        k: v for k, v in want.items() if k not in _VARYING
    }


def test_a_seth_tool_is_created_and_published_but_never_tested_outside_a_run(
    client: TestClient, world: World, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[Any] = []  # a test-only function, wired nowhere on the API
    registry = InternalToolRegistry.of({KEY: ran.append})
    monkeypatch.setattr(worker_main, "internal_tools", lambda: registry)
    h = user_session(client, world.passwords, ADMIN)
    c = h.client
    p1 = {"type": "project", "id": _project(h, "sample")}
    tool = art(c, h, "tools", "board", _seth_tool(), p1)
    for version in ("draft", None):  # the draft, then the published version
        if version is None:
            v1 = publish(c, h, "tools", tool["id"])
            assert v1["state"] == "published"
            version = v1["id"]
        r = c.post(f"{A}/tools/{tool['id']}/test", json={"version_id": version, "args": {}},
                   headers=h)  # fmt: skip
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == REFUSAL
    assert ran == []

    understated = art(c, h, "tools", "board-none", _seth_tool(side="none"), p1)
    r = c.post(f"{A}/tools/{understated['id']}/publish", json={"change_note": "x"}, headers=h)
    assert r.status_code == 422 and codes(r.json()) == {"side_effect_understated"}, r.text
    unknown = art(c, h, "tools", "board-nope", _seth_tool("seth.nope.read"), p1)
    r = c.post(f"{A}/tools/{unknown['id']}/publish", json={"change_note": "x"}, headers=h)
    assert r.status_code == 422 and codes(r.json()) == {"unknown_function"}, r.text


def test_another_client_gets_404_on_a_seth_tool(
    client: TestClient, world: World, owner_session: Session
) -> None:
    a = user_session(client, world.passwords, ADMIN)
    tool = art(a.client, a, "tools", "board", _seth_tool(),
               {"type": "project", "id": _project(a, "sample")})  # fmt: skip
    publish(a.client, a, "tools", tool["id"])
    before = a.client.get(f"{A}/tools/{tool['id']}/versions", headers=a).json()
    b = user_session(client, world.passwords, ADMIN_B)
    missing = str(uuid7())
    for method, suffix, body in (
        ("GET", "", None),
        ("POST", "/test", {"version_id": "draft", "args": {}}),
        ("POST", "/publish", {"change_note": "x"}),
    ):
        _same(
            b.client.request(method, f"{B}/tools/{tool['id']}{suffix}", json=body, headers=b),
            b.client.request(method, f"{B}/tools/{missing}{suffix}", json=body, headers=b),
        )
    assert a.client.get(f"{A}/tools/{tool['id']}/versions", headers=a).json() == before
    assert a.client.get(f"{A}/tools/{tool['id']}", headers=a).status_code == 200


def test_a_p1_only_member_gets_404_on_p2s_seth_tool(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    c = admin.client
    p1, p2 = _project(admin, "sample"), _project(admin, "other")
    mine = art(c, admin, "tools", "board-p1", _seth_tool(), {"type": "project", "id": p1})
    theirs = art(c, admin, "tools", "board-p2", _seth_tool(), {"type": "project", "id": p2})
    role = post(c, f"{A}/roles", admin, {"key": "p1-tools", "name": "P1 tools",
                "permissions": ["workspace:read", "project:read", "tool:read", "tool:test"]})  # fmt: skip
    uid = new_user(c, admin, "demo-a", "p1-only@demo-a.local", "a-long-password-1")
    world.passwords["p1-only@demo-a.local"] = "a-long-password-1"
    admin = user_session(client, world.passwords, ADMIN)
    post(admin.client, f"{A}/role-bindings", admin, {"subject_type": "user", "subject_id": uid,
         "role_id": role["id"], "scope": {"type": "project", "id": p1}})  # fmt: skip
    member = user_session(client, world.passwords, "p1-only@demo-a.local")
    m = member.client
    assert m.get(f"{A}/tools/{mine['id']}", headers=member).status_code == 200  # control
    missing = str(uuid7())
    for method, suffix, body in (
        ("GET", "", None),
        ("POST", "/test", {"version_id": "draft", "args": {}}),
    ):
        _same(
            m.request(method, f"{A}/tools/{theirs['id']}{suffix}", json=body, headers=member),
            m.request(method, f"{A}/tools/{missing}{suffix}", json=body, headers=member),
        )
