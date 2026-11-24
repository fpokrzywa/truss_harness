"""SP5-8b (design section 7): `GET /releases/{release_id}/app-preview`.

A `/2` release answers its rendered files' paths, sizes and SHA-256 digests (never their
content), its screens and its tools; a `/1` release is 409 `release_has_no_app`, a tampered pin
409 `release_source_changed`. Client B's administrator and a member bound only at another project
get the 404 a random id gets.
"""

from __future__ import annotations

import hashlib
from typing import Any  # Any: JSON bodies and manifests
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.appgen.render import GENERATOR_VERSION, render
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.appgen_fixtures import SAMPLE_DRAFT
from tests._support.fullapp import World
from tests._support.p2util import new_user, post, tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import Hdr, user_session
from tests.appgen.helpers import PROJECT
from tests.delivery.app_releases import AppRelease, add_provider, make_app_release
from tests.delivery.releases import make_release

KEYS = {"release_id", "release_digest", "generator_version", "files", "screens", "tools"}
_VARYING = {"instance", "request_id"}
MEMBER = "p1-preview@demo-a.local"


def _scope(owner: Session, slug: str = "demo-a") -> tuple[UUID, UUID]:
    """(tenant id, workspace `main`'s id) of `slug`."""
    tenant = UUID(str(tenant_id(owner, slug)))
    workspace = owner.execute(
        text("SELECT id FROM shifde.workspaces WHERE tenant_id = :t AND slug = 'main'"),
        {"t": tenant},
    ).scalar_one()
    owner.rollback()
    return tenant, workspace


def _app_release(owner_db: Database, owner: Session, **options: Any) -> AppRelease:
    tenant, workspace = _scope(owner)
    provider = add_provider(owner_db, tenant, "fake", None)
    return make_app_release(owner_db, tenant, workspace, provider_id=provider, **options)


def _preview(h: Hdr, release_id: object, org: str = A) -> httpx.Response:
    r: httpx.Response = h.client.get(f"{org}/releases/{release_id}/app-preview", headers=dict(h))
    return r


def _same_as_missing(h: Hdr, release_id: object, org: str = A) -> None:
    got, want = _preview(h, release_id, org), _preview(h, uuid7(), org)
    assert got.status_code == want.status_code == 404, (got.status_code, got.text)
    assert {k: v for k, v in got.json().items() if k not in _VARYING} == {
        k: v for k, v in want.json().items() if k not in _VARYING
    }


def test_a_generated_app_release_previews_file_metadata_screens_and_tools(
    client: TestClient, world: World, owner_db: Database, owner_session: Session
) -> None:
    rel = _app_release(owner_db, owner_session)
    admin = user_session(client, world.passwords, ADMIN)
    r = _preview(admin, rel.id)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == KEYS
    assert out["release_id"] == str(rel.id)
    assert out["release_digest"] == rel.release.digest
    assert out["generator_version"] == GENERATOR_VERSION
    rendered = render(rel.release.manifest, rel.release.digest, rel.resolved())
    paths = [f["path"] for f in out["files"]]
    assert paths == sorted(rendered)
    assert {"app/app.py", "app/config/screens.json", "release.json"} <= set(paths)
    for f in out["files"]:
        assert set(f) == {"path", "size", "sha256"}
        content = rendered[f["path"]]
        assert f["size"] == len(content)
        assert f["sha256"] == hashlib.sha256(content).hexdigest()
    assert rendered["app/agent_loop.py"].decode() not in r.text
    assert out["screens"] == SAMPLE_DRAFT["screens"]
    assert out["tools"] == [
        {
            "name": t["name"],
            "description": t["description"],
            "sql": t["sql"],
            "parameters": sorted(t["parameters"], key=lambda p: p["name"]),
            "max_rows": t["max_rows"],
        }
        for t in sorted(SAMPLE_DRAFT["tools"], key=lambda t: t["name"])
    ]


def test_a_release_without_an_app_is_409(
    client: TestClient, world: World, owner_db: Database, owner_session: Session
) -> None:
    tenant, workspace = _scope(owner_session)
    rel = make_release(owner_db, tenant, workspace)
    r = _preview(user_session(client, world.passwords, ADMIN), rel.id)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "release_has_no_app"


def test_a_changed_pin_is_release_source_changed(
    client: TestClient, world: World, owner_db: Database, owner_session: Session
) -> None:
    def tamper(manifest: dict[str, Any]) -> None:
        manifest["pins"][0]["spec_hash"] = "0" * 64

    rel = _app_release(owner_db, owner_session, edit=tamper)
    r = _preview(user_session(client, world.passwords, ADMIN), rel.id)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "release_source_changed"


def test_b_cannot_preview_as_release(
    client: TestClient, world: World, owner_db: Database, owner_session: Session
) -> None:
    rel = _app_release(owner_db, owner_session)
    admin_b = user_session(client, world.passwords, ADMIN_B)
    _same_as_missing(admin_b, rel.id, B)
    _same_as_missing(admin_b, rel.id, A.replace("demo-a", "no-such-org"))


def test_a_p1_only_member_gets_404_on_a_p2_release(
    client: TestClient, world: World, owner_db: Database, owner_session: Session
) -> None:
    rel = _app_release(owner_db, owner_session)  # in a project of its own (P2)
    admin = user_session(client, world.passwords, ADMIN)
    c = admin.client
    p1 = c.get(f"{A}{PROJECT}", headers=admin).json()["id"]
    permissions = ["workspace:read", "project:read", "packaging:solution:read"]
    role = post(c, f"{A}/roles", admin,
                {"key": "p1-preview", "name": "P1 preview", "permissions": permissions})  # fmt: skip
    uid = new_user(c, admin, "demo-a", MEMBER, "a-long-password-1")
    world.passwords[MEMBER] = "a-long-password-1"
    admin = user_session(client, world.passwords, ADMIN)
    binding = {"subject_type": "user", "subject_id": uid, "role_id": role["id"],
               "scope": {"type": "project", "id": p1}}  # fmt: skip
    post(admin.client, f"{A}/role-bindings", admin, binding)
    member = user_session(client, world.passwords, MEMBER)
    _same_as_missing(member, rel.id)
    # the administrator, bound at the organization, reads it
    assert _preview(admin, rel.id).status_code == 200
