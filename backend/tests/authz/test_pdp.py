"""PDP unit behaviour (default deny, scope inheritance).

Moved from SETH 3ba0e06 `tests/test_pdp_and_cli.py` (the PDP part; the CLI tests move with `cli`).
The seven tests and their assertions are SETH's. The `pdp_env` fixture differs: SETH took the
tenants, workspace, project and user from the seeded `world` (identity/tenancy/cli); here they are
fresh ids, and `_bind` inserts through `owner_insert_bindings` (see conftest.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy.engine import Engine

from shifde.authz.service import PDP, ResourceRef, new_pdp
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal
from shifde.core.db import Database
from shifde.core.errors import Forbidden, NotFound
from shifde.core.ids import uuid7
from tests.authz.conftest import owner_insert_bindings


@pytest.fixture
def pdp_env(seed_engine: Engine, app_db: Database) -> Iterator[dict[str, object]]:
    tenant, other, ws, proj, uid = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    s = app_db.session()
    db.set_tenant(s, tenant)
    yield {
        "s": s,
        "tenant": tenant,
        "other": other,
        "ws": ws,
        "proj": proj,
        "uid": uid,
        "owner": seed_engine,
    }
    s.close()


def _bind(
    owner: Engine,
    env: dict[str, object],
    role: str,
    scope: str,
    scope_id: UUID,
    expires: object = None,
) -> None:
    owner_insert_bindings(
        owner,
        [
            {
                "tenant_id": env["tenant"],
                "subject_id": env["uid"],
                "role_key": role,
                "scope_type": scope,
                "scope_id": scope_id,
                "expires_at": expires,
            }
        ],
    )


def _pdp(env: dict[str, object], admin: bool = False) -> PDP:
    p = Principal(env["uid"], "v@x", "V", admin)  # type: ignore[arg-type]
    return new_pdp(env["s"], p, env["tenant"])  # type: ignore[arg-type]


def test_pdp_default_deny_without_bindings(pdp_env: dict[str, object]) -> None:
    ref = ResourceRef(pdp_env["tenant"], pdp_env["ws"], pdp_env["proj"])  # type: ignore[arg-type]
    d = _pdp(pdp_env).authorize("workspace:read", ref)
    assert not d.allowed and d.reason == "no_binding"
    assert not _pdp(pdp_env).authorize("nonexistent:perm", ref).allowed


def test_pdp_binding_scopes_apply_downwards_only(pdp_env: dict[str, object]) -> None:
    owner = pdp_env["owner"]
    _bind(owner, pdp_env, "workspace_administrator", "workspace", pdp_env["ws"])  # type: ignore[arg-type]
    t, ws, proj = pdp_env["tenant"], pdp_env["ws"], pdp_env["proj"]
    pdp = _pdp(pdp_env)
    in_ws = ResourceRef(t, ws, proj)  # type: ignore[arg-type]
    assert pdp.authorize("project:manage", in_ws).allowed  # descendant project
    assert pdp.authorize("workspace:manage", in_ws).allowed
    assert not pdp.authorize("workspace:create", ResourceRef(t)).allowed  # tenant-level: no  # type: ignore[arg-type]
    assert not pdp.authorize("tenant:admin", ResourceRef(t)).allowed  # type: ignore[arg-type]
    elsewhere = ResourceRef(t, UUID(int=1), UUID(int=2))  # type: ignore[arg-type]
    assert not pdp.authorize("project:read", elsewhere).allowed
    f = pdp.visible_scopes("project:read")
    assert not f.tenant_wide and f.workspace_ids == {ws} and f.covers_project(ws, UUID(int=5))  # type: ignore[arg-type]


def test_pdp_project_scope_and_tenant_scope(pdp_env: dict[str, object]) -> None:
    owner = pdp_env["owner"]
    t, ws, proj = pdp_env["tenant"], pdp_env["ws"], pdp_env["proj"]
    _bind(owner, pdp_env, "project_administrator", "project", proj)  # type: ignore[arg-type]
    pdp = _pdp(pdp_env)
    assert pdp.authorize("project:manage", ResourceRef(t, ws, proj)).allowed  # type: ignore[arg-type]
    assert not pdp.authorize("workspace:read", ResourceRef(t, ws, proj)).allowed  # type: ignore[arg-type]
    assert pdp.visible_scopes("project:read").project_ids == {proj}
    _bind(owner, pdp_env, "viewer", "tenant", t)  # type: ignore[arg-type]
    assert _pdp(pdp_env).visible_scopes("workspace:read").tenant_wide


def test_pdp_expired_binding_is_ignored(pdp_env: dict[str, object]) -> None:
    _bind(
        pdp_env["owner"],  # type: ignore[arg-type]
        pdp_env,
        "viewer",
        "tenant",
        pdp_env["tenant"],  # type: ignore[arg-type]
        expires=utcnow() - timedelta(minutes=1),
    )
    assert not _pdp(pdp_env).authorize("workspace:read", ResourceRef(pdp_env["tenant"])).allowed  # type: ignore[arg-type]


def test_pdp_denies_cross_tenant_resources(pdp_env: dict[str, object]) -> None:
    _bind(pdp_env["owner"], pdp_env, "tenant_administrator", "tenant", pdp_env["tenant"])  # type: ignore[arg-type]
    d = _pdp(pdp_env).authorize("workspace:read", ResourceRef(pdp_env["other"]))  # type: ignore[arg-type]
    assert not d.allowed and d.reason == "cross_tenant"


def test_pdp_platform_actions_need_platform_admin_flag(pdp_env: dict[str, object]) -> None:
    _bind(pdp_env["owner"], pdp_env, "tenant_administrator", "tenant", pdp_env["tenant"])  # type: ignore[arg-type]
    assert (
        not _pdp(pdp_env).authorize("platform:admin").allowed
    )  # tenant admin is not platform admin
    assert _pdp(pdp_env, admin=True).authorize("platform:admin").allowed


def test_pdp_require_maps_invisible_to_404_and_visible_to_403(pdp_env: dict[str, object]) -> None:
    t, ws = pdp_env["tenant"], pdp_env["ws"]
    ref = ResourceRef(t, ws)  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        _pdp(pdp_env).require("workspace:manage", ref, hide_unless="workspace:read")
    _bind(pdp_env["owner"], pdp_env, "viewer", "tenant", t)  # type: ignore[arg-type]
    with pytest.raises(Forbidden):
        _pdp(pdp_env).require("workspace:manage", ref, hide_unless="workspace:read")
    _pdp(pdp_env).require("workspace:read", ref)
