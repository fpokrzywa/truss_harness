"""P2 audit: privileged operations are audited, the per-tenant hash chain verifies, tampering shows."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from shifde.audit import service as audit
from shifde.cli import main as cli
from shifde.core.context import RequestContext
from shifde.core.db import Database
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login, post, system_role, tenant_id

A = "/api/v1/orgs/demo-a"
ADMIN = "admin@demo-a.local"
runner = CliRunner()


def _scenario(client: TestClient, world: World, owner: Session) -> dict[str, str]:
    """Exercise every privileged operation family once; returns useful ids."""
    # Unified setup (B-928, B-1401..B-1406, B-2251): people and groups are created and filled by
    # the platform administrator; the client administrator binds the group. SETH did all of it as
    # the client administrator.
    ph = login(client, world.passwords, "platform@shifde.local")
    group = post(client, "/api/v1/platform/groups", ph, {"slug": "g1", "name": "g1"})
    created = post(client, f"{A}/users", ph, {"email": "audited@example.com", "display_name": "A"})
    uid = created["user"]["user"]["id"]
    h = login(client, world.passwords, ADMIN)
    tid = str(tenant_id(owner, "demo-a"))
    role = post(
        client, f"{A}/roles", h, {"key": "r1", "name": "r1", "permissions": ["workspace:read"]}
    )
    binding = post(
        client,
        f"{A}/role-bindings",
        h,
        {
            "subject_type": "group",
            "subject_id": group["id"],
            "role_id": role["id"],
            "scope": {"type": "tenant", "id": tid},
        },
    )
    ph = login(client, world.passwords, "platform@shifde.local")
    members = client.put(
        f"/api/v1/platform/groups/{group['id']}/members",
        json={"user_ids": [uid], "row_version": group["row_version"]},
        headers=ph,
    )
    assert members.status_code == 200, members.text
    h = login(client, world.passwords, ADMIN)
    sa = post(client, f"{A}/service-accounts", h, {"name": "bot"})
    key = post(
        client,
        f"{A}/api-keys",
        h,
        {
            "name": "k",
            "principal_type": "service_account",
            "principal_id": sa["id"],
            "scopes": ["workspace:read"],
            "expires_in_days": 5,
        },
    )
    rotated = post(client, f"{A}/api-keys/{key['api_key']['id']}/rotate", h, {"grace_hours": 1})
    client.post(f"{A}/api-keys/{rotated['api_key']['id']}/revoke", headers=h)
    secret = post(client, f"{A}/secrets", h, {"name": "db-pass", "value": "s3cr3t-value-xyz"})
    client.post(f"{A}/secrets/{secret['id']}/rotate", json={"value": "s3cr3t-value-2"}, headers=h)
    idp = post(
        client,
        f"{A}/idps",
        h,
        {
            "display_name": "i",
            "issuer": "https://i.example",
            "discovery_url": "https://i.example/.well-known/openid-configuration",
            "client_id": "c",
        },
    )
    client.delete(f"{A}/role-bindings/{binding['id']}", headers=h)
    row = next(
        u for u in client.get(f"{A}/users", headers=h).json()["items"] if u["user"]["id"] == uid
    )
    client.patch(
        f"{A}/users/{uid}",
        json={"status": "suspended", "row_version": row["row_version"]},
        headers=h,
    )
    return {"idp": idp["id"], "group": group["id"], "uid": uid}


def test_every_privileged_operation_is_audited_and_the_chain_verifies(
    client: TestClient, world: World, owner_session: Session
) -> None:
    _scenario(client, world, owner_session)
    h = login(client, world.passwords, ADMIN)
    events = client.get(f"{A}/audit-events?category=security&limit=200", headers=h).json()["items"]
    actions = {e["action"] for e in events}
    expected = {
        "authz.role.created",
        "identity.user.created",
        "authz.group.bound",  # SETH: authz.binding.created; a group binding has its own action
        "authz.binding.deleted",
        "identity.group.members_changed",
        "identity.service_account.created",
        "identity.api_key.created",
        "identity.api_key.rotated",
        "identity.api_key.revoked",
        "secret.created",
        "secret.rotated",
        "identity.idp.created",
        "identity.user.suspended",
        "identity.login.succeeded",
    }
    assert expected <= actions, expected - actions
    # a platform group is created before any client binds it: an installation-level event
    # (B-1402), so it is not in the client's own list; the client sees its later changes
    assert "identity.group.created" not in actions
    assert (
        owner_session.execute(
            text(
                "SELECT count(*) FROM shifde.audit_events "
                "WHERE action = 'identity.group.created' AND tenant_id IS NULL"
            )
        ).scalar_one()
        == 1
    )
    assert all(e["occurred_at"] and e["outcome"] and e["request_id"] for e in events)
    verify = client.get(f"{A}/audit-events/verify", headers=h).json()
    assert (
        verify["ok"] is True
        and verify["checked"] >= len(expected)
        and verify["first_broken_id"] is None
    )
    # the secret values and key secrets never reach audit payloads
    blob = str(owner_session.execute(text("SELECT details FROM shifde.audit_events")).all())
    for needle in ("s3cr3t-value-xyz", "s3cr3t-value-2", "shf_test_"):
        assert needle not in blob


def test_audit_list_filters_and_cursor(
    client: TestClient, world: World, owner_session: Session
) -> None:
    _scenario(client, world, owner_session)
    h = login(client, world.passwords, ADMIN)
    page1 = client.get(f"{A}/audit-events?limit=3", headers=h).json()
    assert len(page1["items"]) == 3 and page1["next_cursor"]
    page2 = client.get(f"{A}/audit-events?limit=3&cursor={page1['next_cursor']}", headers=h).json()
    ids = [e["id"] for e in page1["items"] + page2["items"]]
    assert len(set(ids)) == 6 and ids == sorted(ids, reverse=True)  # newest first, no overlap
    only = client.get(f"{A}/audit-events?action=identity.api_key.rotated", headers=h).json()[
        "items"
    ]
    assert [e["action"] for e in only] == ["identity.api_key.rotated"]
    by_actor = client.get(f"{A}/audit-events?actor={ADMIN}&category=security", headers=h).json()[
        "items"
    ]
    assert by_actor and all(e["actor"] == ADMIN for e in by_actor)
    admin_id = by_actor[0]["actor_id"]
    assert client.get(f"{A}/audit-events?actor={admin_id}", headers=h).json()["items"]
    future = client.get(f"{A}/audit-events?from=2999-01-01T00:00:00Z", headers=h).json()["items"]
    assert future == []
    assert client.get(f"{A}/audit-events?category=nonsense", headers=h).status_code == 422
    tenancy = client.get(f"{A}/audit-events?category=tenancy", headers=h).json()["items"]
    assert all(e["action"].startswith("tenancy.") for e in tenancy)


@contextmanager
def _trigger_off(owner: Session) -> Iterator[None]:
    owner.execute(
        text("ALTER TABLE shifde.audit_events DISABLE TRIGGER trg_audit_events_append_only")
    )
    owner.commit()
    try:
        yield
    finally:
        owner.rollback()
        owner.execute(
            text("ALTER TABLE shifde.audit_events ENABLE TRIGGER trg_audit_events_append_only")
        )
        owner.commit()


def _verify(client: TestClient, world: World) -> dict[str, object]:
    h = login(client, world.passwords, ADMIN)
    return client.get(f"{A}/audit-events/verify", headers=h).json()


def test_tampering_with_an_event_is_detected(
    client: TestClient, world: World, owner_session: Session
) -> None:
    _scenario(client, world, owner_session)
    assert _verify(client, world)["ok"] is True
    victim = owner_session.execute(
        text("SELECT id FROM shifde.audit_events WHERE action = 'identity.api_key.revoked'")
    ).scalar_one()
    with _trigger_off(owner_session):
        owner_session.execute(
            text(
                "UPDATE shifde.audit_events SET details = details || '{\"forged\": true}' WHERE id = :i"
            ),
            {"i": victim},
        )
        owner_session.commit()
        result = _verify(client, world)
    assert result["ok"] is False and result["first_broken_id"] == str(victim)


def test_deleting_or_reordering_events_is_detected(
    client: TestClient, world: World, owner_session: Session
) -> None:
    _scenario(client, world, owner_session)
    ids = (
        owner_session.execute(
            text(
                "SELECT id FROM shifde.audit_events WHERE chain_seq IS NOT NULL "
                "AND tenant_id = :t ORDER BY chain_seq"
            ),
            {"t": tenant_id(owner_session, "demo-a")},
        )
        .scalars()
        .all()
    )
    middle = ids[len(ids) // 2]
    with _trigger_off(owner_session):
        owner_session.execute(text("DELETE FROM shifde.audit_events WHERE id = :i"), {"i": middle})
        owner_session.commit()
        result = _verify(client, world)
    assert result["ok"] is False
    assert result["first_broken_id"] == str(
        ids[len(ids) // 2 + 1]
    )  # the gap is reported at its successor


def test_hash_is_the_chain_of_prev_hash_and_canonical_event(
    client: TestClient, world: World, owner_session: Session
) -> None:
    _scenario(client, world, owner_session)
    rows = owner_session.execute(
        text(
            "SELECT chain_seq, prev_hash, hash FROM shifde.audit_events "
            "WHERE chain_seq IS NOT NULL AND tenant_id = :t ORDER BY chain_seq"
        ),
        {"t": tenant_id(owner_session, "demo-a")},
    ).all()
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1))
    assert rows[0][1] is None
    for prev, cur in zip(rows, rows[1:], strict=False):
        assert bytes(cur[1]) == bytes(prev[2]) and len(bytes(cur[2])) == 32


def test_concurrent_appends_keep_one_linear_chain(
    owner_url: str, world: World, owner_session: Session
) -> None:
    tid = tenant_id(owner_session, "demo-a")
    owner_session.execute(text("TRUNCATE shifde.audit_events"))
    owner_session.commit()
    db = Database(owner_url, pool_size=8, max_overflow=0, verify_role=False)  # the schema owner

    def work(i: int) -> None:
        with db.session() as s:
            audit.record(
                s,
                RequestContext(request_id=f"req-{i:04d}"),
                action="test.concurrent",
                tenant_id=tid,
                details={"i": i},
            )
            s.commit()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(40)))
        with db.session() as s:
            result = audit.verify_chain(s, tid)
            assert result.ok and result.checked == 40
            seqs = (
                s.execute(text("SELECT chain_seq FROM shifde.audit_events ORDER BY chain_seq"))
                .scalars()
                .all()
            )
            assert seqs == list(range(1, 41))
    finally:
        db.dispose()


def test_audit_details_are_scrubbed_and_installation_events_are_unchained(
    owner_session: Session, world: World
) -> None:
    tid = tenant_id(owner_session, "demo-a")
    ctx = RequestContext(request_id="scrub-1")
    audit.record(
        owner_session,
        ctx,
        action="test.scrub",
        tenant_id=tid,
        details={
            "password": "hunter2",
            "api_key": "k",
            "key_id": "ok-to-keep",
            "note": "header was Bearer abcdefghijklmnop",
            "nested": {"client_secret": "zzz"},
        },
    )
    audit.record(owner_session, ctx, action="identity.login.failed", tenant_id=None, details={})
    owner_session.commit()
    d = owner_session.execute(
        text("SELECT details FROM shifde.audit_events WHERE action = 'test.scrub'")
    ).scalar_one()
    assert (
        d["password"] == "[REDACTED]"
        and d["api_key"] == "[REDACTED]"
        and d["key_id"] == "ok-to-keep"
    )
    assert "abcdefghijklmnop" not in d["note"] and d["nested"]["client_secret"] == "[REDACTED]"
    assert (
        owner_session.execute(
            text("SELECT chain_seq FROM shifde.audit_events WHERE action = 'identity.login.failed'")
        ).scalar_one()
        is None
    )


# ---------------------------------------------------------------- denials + privileged allows
def test_denials_and_privileged_allows_are_recorded(
    client: TestClient, world: World, owner_session: Session
) -> None:
    vh = login(client, world.passwords, "viewer@demo-a.local")
    assert client.get(f"{A}/users", headers=vh).status_code == 403
    assert (
        client.post(
            f"{A}/roles", json={"key": "x", "name": "x", "permissions": []}, headers=vh
        ).status_code
        == 403
    )
    denied = owner_session.execute(
        text(
            "SELECT action, decision, reason_codes, principal_type FROM shifde.policy_decisions WHERE decision = 'deny' ORDER BY at"
        )
    ).all()
    assert [(d[0], d[1], d[2], d[3]) for d in denied] == [
        ("member:read", "deny", ["no_binding"], "user"),
        ("role:manage", "deny", ["no_binding"], "user"),
    ]
    ev = owner_session.execute(
        text(
            "SELECT outcome, details->>'permission', details->>'reason' FROM shifde.audit_events WHERE action = 'authz.denied' ORDER BY id"
        )
    ).all()
    assert [tuple(e) for e in ev] == [
        ("denied", "member:read", "no_binding"),
        ("denied", "role:manage", "no_binding"),
    ]
    ah = login(client, world.passwords, ADMIN)
    post(client, f"{A}/roles", ah, {"key": "ok", "name": "ok", "permissions": []})
    allowed = owner_session.execute(
        text(
            "SELECT action, matched_binding_ids FROM shifde.policy_decisions WHERE decision = 'allow'"
        )
    ).all()
    assert [a[0] for a in allowed] == ["role:manage"] and len(
        allowed[0][1]
    ) == 1  # privileged allow
    # a plain read allow is not logged
    client.get(f"{A}/roles", headers=ah)
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.policy_decisions WHERE decision = 'allow'")
        ).scalar_one()
        == 1
    )
    # denied events are visible in the tenant audit read API (for auditors/admins)
    listed = client.get(f"{A}/audit-events?action=authz.denied", headers=ah).json()["items"]
    assert len(listed) == 2 and all(e["outcome"] == "denied" for e in listed)


def test_key_scope_denial_is_logged_with_its_reason(
    client: TestClient, world: World, owner_session: Session
) -> None:
    from tests._support.p2util import bearer, make_key

    h = login(client, world.passwords, ADMIN)
    _, created = make_key(client, h, owner_session, scopes=["workspace:read"])
    client.cookies.clear()
    assert (
        client.post(
            f"{A}/workspaces", json={"slug": "x", "name": "x"}, headers=bearer(created["secret"])
        ).status_code
        == 403
    )
    row = owner_session.execute(
        text(
            "SELECT reason_codes, principal_type FROM shifde.policy_decisions WHERE action = 'workspace:create' AND decision = 'deny'"
        )
    ).one()
    assert row[0] == ["key_scope"] and row[1] == "service_account"


# ---------------------------------------------------------------- CLI
@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


def test_cli_audit_verify_reports_ok_then_broken(
    cli_env: Settings, client: TestClient, world: World, owner_session: Session
) -> None:
    _scenario(client, world, owner_session)
    ok = runner.invoke(cli.app, ["audit", "verify", "--org", "demo-a"])
    assert ok.exit_code == 0 and "audit chain OK" in ok.output
    assert runner.invoke(cli.app, ["audit", "verify", "--org", "nope"]).exit_code == 1
    victim = owner_session.execute(
        text("SELECT id FROM shifde.audit_events WHERE chain_seq = 2")
    ).scalar_one()
    with _trigger_off(owner_session):
        owner_session.execute(
            text("UPDATE shifde.audit_events SET outcome = 'failure' WHERE id = :i"), {"i": victim}
        )
        owner_session.commit()
        bad = runner.invoke(cli.app, ["audit", "verify", "--org", "demo-a"])
    assert bad.exit_code == 2 and str(victim) in bad.output
    _ = system_role
