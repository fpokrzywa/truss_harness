"""Shortcuts of the assist tests: the organization switch, a specialists' model on the fake
provider, a second project, install and the specialists list through the routes, a member bound
only at one project, schema-owner reads of the assist and build rows, and seeded proposals."""

from __future__ import annotations

import json
from typing import Any  # Any: JSON bodies and row values

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from tests._support.fullapp import World
from tests._support.p2util import new_user, post, tenant_id
from tests._support.p3util import ADMIN, A, fake_model_stack
from tests._support.p5util import Hdr, user_session

PROJECT = "/workspaces/main/projects/sample"
P2 = "/workspaces/main/projects/other"
NO_PROJECT = "/workspaces/main/projects/no-such-project"
DEVELOPER_ROLE = "01900000-0000-7000-8000-000000000004"
PASSWORD = "a-long-password-1"


def enable(h: Hdr, org: str = A) -> dict[str, Any]:
    out: dict[str, Any] = post(h.client, f"{org}/assist/enable", h, None, 200)
    return out


def model(h: Hdr, org: str = A, **over: Any) -> str:
    """An active model of `org` on the fake provider (it answers with a fixed text)."""
    ids = fake_model_stack(h.client, h, org=org, conn_name=f"assist-{uuid7().hex[-8:]}", **over)
    return ids["model"]


def add_project(h: Hdr, slug: str = "other", org: str = A) -> str:
    made = post(h.client, f"{org}/workspaces/main/projects", h, {"slug": slug, "name": slug})
    return str(made["id"])


def install(
    h: Hdr, model_id: str, org: str = A, project: str = PROJECT, keys: list[str] | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"model_id": model_id, **({"keys": keys} if keys is not None else {})}
    sent: httpx.Response = h.client.post(f"{org}{project}/assist/install", headers=h, json=body)
    return sent


def installed(
    h: Hdr, model_id: str, org: str = A, project: str = PROJECT, keys: list[str] | None = None
) -> list[dict[str, Any]]:
    r = install(h, model_id, org, project, keys)
    assert r.status_code == 200, r.text
    out: list[dict[str, Any]] = r.json()
    return out


def specialists(h: Hdr, org: str = A, project: str = PROJECT) -> httpx.Response:
    sent: httpx.Response = h.client.get(f"{org}{project}/assist/specialists", headers=h)
    return sent


def toggle(h: Hdr, key: str, action: str, org: str = A, project: str = PROJECT) -> httpx.Response:
    url = f"{org}{project}/assist/specialists/{key}/{action}"
    sent: httpx.Response = h.client.post(url, headers=h)
    return sent


def project_member(
    client: TestClient, world: World, email: str, project_id: str, role_id: str = DEVELOPER_ROLE
) -> Hdr:
    """A new member of demo-a bound only at `project_id` with `role_id` (system developer by
    default); signs the administrator back in afterwards."""
    admin = user_session(client, world.passwords, ADMIN)
    uid = new_user(admin.client, admin, "demo-a", email, PASSWORD)
    world.passwords[email] = PASSWORD
    admin = user_session(client, world.passwords, ADMIN)
    binding = {"subject_type": "user", "subject_id": uid, "role_id": role_id,
               "scope": {"type": "project", "id": project_id}}  # fmt: skip
    post(admin.client, f"{A}/role-bindings", admin, binding)
    return user_session(client, world.passwords, email)


def rows(owner: Session, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    owner.rollback()
    out = [tuple(r) for r in owner.execute(text(sql), params).all()]
    owner.rollback()
    return out


def build_state(owner: Session, org_slug: str = "demo-a") -> list[tuple[Any, ...]]:
    """Every artifact and version of client `org_slug`, and its assist rows (schema owner)."""
    t = tenant_id(owner, org_slug)
    return rows(
        owner,
        "SELECT 'a', a.id::text, a.key, a.status, a.row_version::text FROM shifde.artifacts a "
        "WHERE a.tenant_id = :t UNION ALL SELECT 'v', v.id::text, v.state, v.version_no::text, "
        "v.spec::text FROM shifde.artifact_versions v WHERE v.tenant_id = :t UNION ALL "
        "SELECT 's', s.id::text, s.status, s.install_progress::text, s.agent_version_id::text "
        "FROM shifde.assist_specialists s WHERE s.tenant_id = :t ORDER BY 1, 2",
        t=t,
    )


def set_settings(owner: Session, org_slug: str, **values: Any) -> None:
    """Write `assist_settings` columns of `org_slug` as the schema owner (upsert)."""
    t = tenant_id(owner, org_slug)
    owner.rollback()
    cols = ", ".join(values)
    marks = ", ".join(f":{k}" for k in values)
    sets = ", ".join(f"{k} = excluded.{k}" for k in values)
    owner.execute(
        text(f"INSERT INTO shifde.assist_settings (tenant_id, {cols}) VALUES (:t, {marks}) "
             f"ON CONFLICT (tenant_id) DO UPDATE SET {sets}"),
        {"t": t, **values},
    )  # fmt: skip
    owner.commit()


# ------------------------------------------------------------------ SP7-4: rows of the proposals
def user_of(owner: Session, email: str) -> str:
    [(uid,)] = rows(owner, "SELECT id::text FROM shifde.users WHERE primary_email = :e", e=email)
    return str(uid)


def project_of(owner: Session, org_slug: str, slug: str = "sample") -> tuple[str, str]:
    """(workspace id, project id) of `org_slug`'s project `main/<slug>`."""
    [(ws, proj)] = rows(
        owner,
        "SELECT p.workspace_id::text, p.id::text FROM shifde.projects p JOIN shifde.workspaces w "
        "ON w.id = p.workspace_id WHERE p.tenant_id = :t AND w.slug = 'main' AND p.slug = :s",
        t=tenant_id(owner, org_slug),
        s=slug,
    )
    return str(ws), str(proj)


def seed_proposal(
    owner: Session,
    org_slug: str,
    kind: str,
    payload: dict[str, Any],
    *,
    project: str = "sample",
    email: str = "admin@demo-a.local",
    age: str = "0 seconds",
) -> tuple[str, str]:
    """A `routed` request of `email` in `org_slug`'s project and one `open` proposal of it of
    `kind` with `payload`, both created `age` ago (schema owner); (request id, proposal id)."""
    t = tenant_id(owner, org_slug)
    ws, proj = project_of(owner, org_slug, project)
    request_id, proposal_id = uuid7(), uuid7()
    owner.rollback()
    owner.execute(
        text("INSERT INTO shifde.assist_requests (tenant_id, id, workspace_id, project_id, "
             "requested_by, text, status, specialist_key, route_method, run_id, created_at) "
             "VALUES (:t, :id, :w, :p, :u, 'deploy it', 'routed', 'deployment', 'rule', :run, "
             "now() - CAST(:age AS interval))"),
        {"t": t, "id": request_id, "w": ws, "p": proj, "u": user_of(owner, email), "run": uuid7(),
         "age": age},
    )  # fmt: skip
    owner.execute(
        text("INSERT INTO shifde.assist_proposals (tenant_id, id, request_id, run_id, step_key, "
             "kind, payload, created_at) VALUES (:t, :id, :r, :run, :k, :kind, "
             "CAST(:payload AS jsonb), now() - CAST(:age AS interval))"),
        {"t": t, "id": proposal_id, "r": request_id, "run": uuid7(), "k": uuid7().hex,
         "kind": kind, "payload": json.dumps(payload), "age": age},
    )  # fmt: skip
    owner.commit()
    return str(request_id), str(proposal_id)


def proposal_row(owner: Session, proposal_id: str) -> dict[str, Any]:
    owner.rollback()
    row = owner.execute(
        text("SELECT * FROM shifde.assist_proposals WHERE id = :p"), {"p": proposal_id}
    ).mappings().one()  # fmt: skip
    owner.rollback()
    return dict(row)


def set_payload(owner: Session, proposal_id: str, payload: dict[str, Any]) -> None:
    """Rewrite a stored proposal's payload (schema owner): state that changed since."""
    owner.rollback()
    owner.execute(
        text("UPDATE shifde.assist_proposals SET payload = CAST(:p AS jsonb) WHERE id = :i"),
        {"p": json.dumps(payload), "i": proposal_id},
    )
    owner.commit()


def decide(h: Hdr, proposal_id: str, action: str, org: str = A) -> httpx.Response:
    """`POST .../assist/proposals/{id}/accept` or `/dismiss` (body `{}`)."""
    sent: httpx.Response = h.client.post(
        f"{org}/assist/proposals/{proposal_id}/{action}", headers=h, json={}
    )
    return sent
