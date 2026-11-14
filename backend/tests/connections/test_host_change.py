"""C2 (findings C2; REQ F-01, SEC Major-1; FR-CON-011; charter 5 item 3): repointing a connection
cannot send its bound secret without `secret:use` on that secret.

SETH 3ba0e06 `tests/test_r1_host_change.py`, moved with its assertions (DECISIONS B-1506). SETH's
seeded world becomes the tenancy route-test world: `ADMIN` is A's owner (`tenant_administrator`),
the custom-role manager is `routes.limited_user`, the `/secrets`, `/roles` and `/role-bindings`
calls (admin routes, not ported yet) are `routes.secret` and `routes.grant`. Deliveries are
counted at local listeners, not inferred from status codes. With the update path's
`_require_secret_use_for_host_change` removed (the defect the auditors reproduced) the first test
fails: the PATCH answers 200 and the test call delivers the secret to `evil`.
"""

from __future__ import annotations

import json
from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.testservers import RestTestServer
from shifde.connections import repository as connections_repository
from shifde.core.db import Database, set_tenant
from shifde.core.settings import Settings
from tests.connections.routes import (
    REST_TOKEN,
    activate,
    as_user,
    connection,
    grant,
    limited_user,
    org,
    rest_body,
    secret,
    system_role_user,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

MANAGER_PERMS = [
    "workspace:read",
    "project:read",
    "connection:read",
    "connection:manage",
    "connection:test",
]


def _deliveries(server: RestTestServer) -> int:
    return len([r for r in server.requests if r["authenticated"]])


class Setup:
    def __init__(
        self,
        client: TestClient,
        world: World,
        people: People,
        app_db: Database,
        owner_db: Database,
        settings: Settings,
        good: RestTestServer,
    ) -> None:
        self.client, self.world, self.people, self.good = client, world, people, good
        self.app_db, self.owner_db, self.settings = app_db, owner_db, settings
        self.A = org(world.a.slug)
        self.secret_id = secret(app_db, settings, world.a.tenant, REST_TOKEN)
        admin = self.as_admin()
        self.conn = connection(client, admin, rest_body(good, self.secret_id), self.A)
        activate(client, admin, self.conn["id"], self.A)
        self.manager, self.manager_id = limited_user(
            owner_db, world.a.tenant, MANAGER_PERMS, label="manager"
        )

    def as_admin(self) -> dict[str, str]:
        return as_user(self.client, self.people.owner_a)

    def as_manager(self) -> dict[str, str]:
        return as_user(self.client, self.manager)

    @property
    def cid(self) -> str:
        return str(self.conn["id"])

    def row(self, h: dict[str, str], cid: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = self.client.get(
            f"{self.A}/connections/{cid or self.cid}", headers=h
        ).json()
        return out

    def patch(self, h: dict[str, str], cid: str, body: dict[str, Any]) -> Any:  # Any: a response
        row = self.row(h, cid)
        return self.client.patch(
            f"{self.A}/connections/{cid}",
            headers=h,
            json={**body, "row_version": row["row_version"]},
        )


@pytest.fixture
def setup(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> Setup:
    return Setup(client, world, people, app_db, owner_db, settings, rest_server)


# ------------------------------------------------------------------ the F-01 reproduction
def test_a_role_without_secret_use_cannot_repoint_a_credentialed_connection(
    setup: Setup, evil: RestTestServer
) -> None:
    c, A, hm = setup.client, setup.A, setup.as_manager()
    r = setup.patch(
        hm, setup.cid, {"config": {"base_url": evil.base_url, "health_path": "/health"}}
    )
    assert r.status_code == 403, r.text
    assert "secret:use" in r.text
    # nothing changed: same host, still active, still bound
    row = setup.row(hm)
    assert row["config"]["base_url"] == setup.good.base_url
    assert row["status"] == "active" and row["secret_set"] is True
    # every path that would send the secret reaches only the original host
    assert c.post(f"{A}/connections/{setup.cid}/test", headers=hm).status_code == 200
    assert c.post(f"{A}/connections/{setup.cid}/discover", headers=hm).status_code in (200, 422)
    assert evil.requests == [] and _deliveries(evil) == 0


def test_the_same_role_can_still_edit_what_does_not_decide_the_destination(
    setup: Setup, evil: RestTestServer
) -> None:
    hm = setup.as_manager()
    cfg = {
        "base_url": setup.good.base_url,
        "health_path": "/health2",
        "default_headers": {"X-Env": "blue"},
    }
    r = setup.patch(hm, setup.cid, {"config": cfg, "name": "renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft" and r.json()["secret_set"] is True  # re-test, no detach
    assert evil.requests == []


def test_secret_use_on_another_secret_does_not_authorize_the_host_change(
    setup: Setup, evil: RestTestServer
) -> None:
    ws = setup.world.a.ws
    other = secret(
        setup.app_db,
        setup.settings,
        setup.world.a.tenant,
        "other-secret-value-1",
        scope_type="workspace",
        scope_id=ws,
    )
    grant(
        setup.owner_db,
        setup.world.a.tenant,
        setup.manager_id,
        ["secret:use", "secret:read"],
        scope_type="workspace",
        scope_id=ws,
    )
    hm = setup.as_manager()
    r = setup.patch(
        hm, setup.cid, {"config": {"base_url": evil.base_url, "health_path": "/health"}}
    )
    assert r.status_code == 403, r.text  # secret:use is held, but not on the BOUND secret
    assert other != setup.secret_id
    assert evil.requests == []


def test_a_role_with_secret_use_can_repoint_and_the_connection_must_be_retested(
    setup: Setup, evil: RestTestServer
) -> None:
    c, A = setup.client, setup.A
    grant(setup.owner_db, setup.world.a.tenant, setup.manager_id, ["secret:use", "secret:read"])
    hm = setup.as_manager()
    r = setup.patch(
        hm, setup.cid, {"config": {"base_url": evil.base_url, "health_path": "/health"}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft" and r.json()["secret_set"] is True
    # activation still needs a fresh passing test of THIS definition
    stale = c.post(
        f"{A}/connections/{setup.cid}/status",
        headers=hm,
        json={"status": "active", "row_version": r.json()["row_version"]},
    )
    assert stale.status_code == 409
    assert evil.requests == []
    activate(c, hm, setup.cid, A)  # test + activate: the authorized holder chose this host
    assert c.post(f"{A}/connections/{setup.cid}/discover", headers=hm).status_code in (200, 422)
    assert _deliveries(evil) >= 2


@pytest.mark.parametrize("role_key", ["workspace_administrator", "project_administrator"])
def test_seeded_scope_administrators_and_the_tenant_admin_still_repoint(
    setup: Setup, evil: RestTestServer, role_key: str
) -> None:
    email = system_role_user(setup.owner_db, setup.world.a.tenant, role_key)
    h = as_user(setup.client, email)
    r = setup.patch(h, setup.cid, {"config": {"base_url": evil.base_url, "health_path": "/health"}})
    assert r.status_code == 200, r.text
    back = setup.patch(
        setup.as_admin(),
        setup.cid,
        {"config": {"base_url": setup.good.base_url, "health_path": "/health"}},
    )
    assert back.status_code == 200, back.text  # the tenant administrator too


def test_an_explicit_detach_together_with_a_host_change_sends_no_secret(
    setup: Setup, evil: RestTestServer
) -> None:
    c, A, hm = setup.client, setup.A, setup.as_manager()
    r = setup.patch(
        hm,
        setup.cid,
        {
            "config": {"base_url": evil.base_url, "health_path": "/health"},
            "secret_id": None,
            "auth_method": "none",
        },
    )
    assert r.status_code == 200 and r.json()["secret_set"] is False  # the caller said so
    assert c.post(f"{A}/connections/{setup.cid}/test", headers=hm).status_code == 200
    assert _deliveries(evil) == 0  # reached the listener, but without the credential
    assert evil.requests  # (it was contacted, anonymously)


def test_a_detached_secret_cannot_be_rebound_after_the_host_change_without_secret_use(
    setup: Setup, evil: RestTestServer
) -> None:
    """The two-step path around the rule (detach, repoint, bind again) ends at the bind: binding a
    secret needs `secret:use` on it (`_check_secret`), so the credential never reaches `evil`."""
    c, A, hm = setup.client, setup.A, setup.as_manager()
    moved = setup.patch(
        hm,
        setup.cid,
        {
            "config": {"base_url": evil.base_url, "health_path": "/health"},
            "secret_id": None,
            "auth_method": "none",
        },
    )
    assert moved.status_code == 200, moved.text
    rebind = setup.patch(
        hm,
        setup.cid,
        {"secret_id": setup.secret_id, "auth_method": "bearer", "read_only_confirmed": True},
    )
    assert rebind.status_code == 404, rebind.text  # hidden: the manager has no secret:read
    assert setup.row(hm)["secret_set"] is False
    # a new connection to `evil` naming the secret is refused the same way
    body = {**rest_body(evil, setup.secret_id, name="to-evil")}
    connection(c, hm, body, A, status=404)
    assert c.post(f"{A}/connections/{setup.cid}/test", headers=hm).status_code == 200
    assert _deliveries(evil) == 0


# ------------------------------------------------------------------ the read-to-write race
def _write_after_read(
    monkeypatch: pytest.MonkeyPatch,
    owner_db: Database,
    tenant: UUID,
    cid: str,
    assignments: str,
    params: dict[str, Any],
) -> list[bool]:
    """Connections review 2 item 1 (B-1511): right after the service's next read of `cid`, a
    concurrent writer commits `assignments` (a bind or a definition change, with the version bump
    every service write makes), so the request under test writes over a row that has moved since it
    was read. Returns a list that holds one entry once the interleaved write has happened."""
    fired: list[bool] = []
    real = connections_repository.get_connection

    def read_then_concurrent_write(
        session: Session, connection_id: UUID, *, tenant_id: UUID
    ) -> Any:  # Any: the repository's row mapping
        row = real(session, connection_id, tenant_id=tenant_id)
        if not fired and str(connection_id) == cid:
            fired.append(True)
            with owner_db.session() as o:
                set_tenant(o, tenant)
                o.execute(
                    text(
                        f"UPDATE shifde.connections SET {assignments}, "
                        "row_version = row_version + 1 WHERE id = :i"
                    ),
                    {**params, "i": cid},
                )
                o.commit()
        return row

    monkeypatch.setattr(connections_repository, "get_connection", read_then_concurrent_write)
    return fired


# the caller sends no `row_version`, or the version the concurrent write produced (R+1, read
# after it): either way this request's checks ran on R, so its write must not land
SENT_VERSIONS = ["none", "after_concurrent_write"]


def _sent(mode: str, read: int) -> dict[str, int]:
    return {} if mode == "none" else {"row_version": read + 1}


@pytest.mark.parametrize("sent", SENT_VERSIONS)
def test_a_secret_bound_between_the_managers_read_and_write_stops_the_repoint(
    setup: Setup, evil: RestTestServer, monkeypatch: pytest.MonkeyPatch, sent: str
) -> None:
    """A manager without `secret:use` repoints a secretless connection (no `row_version` sent)
    while the owner binds a secret between the manager's read and write: the manager's host-change
    check saw no secret, so the write must not land (409) and the secret never reaches `evil`."""
    c, A = setup.client, setup.A
    plain = connection(c, setup.as_admin(), rest_body(setup.good, None, name="plain"), A)
    pid = str(plain["id"])
    hm = setup.as_manager()
    fired = _write_after_read(
        monkeypatch,
        setup.owner_db,
        setup.world.a.tenant,
        pid,
        "secret_ref_id = CAST(:s AS uuid), auth_method = 'bearer'",
        {"s": setup.secret_id},
    )
    r = c.patch(
        f"{A}/connections/{pid}",
        headers=hm,
        json={
            "config": {"base_url": evil.base_url, "health_path": "/health"},
            **_sent(sent, plain["row_version"]),
        },
    )
    assert fired, "the concurrent bind did not run between the read and the write"
    monkeypatch.undo()
    assert r.status_code == 409, r.text
    row = setup.row(hm, pid)
    assert row["config"]["base_url"] == setup.good.base_url and row["secret_set"] is True
    assert c.post(f"{A}/connections/{pid}/test", headers=hm).status_code == 200
    assert evil.requests == [] and _deliveries(evil) == 0


@pytest.mark.parametrize("sent", SENT_VERSIONS)
def test_a_definition_changed_between_the_read_and_write_of_an_activation_is_not_activated(
    setup: Setup, evil: RestTestServer, monkeypatch: pytest.MonkeyPatch, sent: str
) -> None:
    """`set_status` without `row_version`: the passing test matched the definition it read; a
    repoint committed before its write leaves a definition nobody tested, which stays a draft."""
    c, A, admin = setup.client, setup.A, setup.as_admin()
    plain = connection(c, admin, rest_body(setup.good, setup.secret_id, name="tested"), A)
    pid = str(plain["id"])
    tested = c.post(f"{A}/connections/{pid}/test", headers=admin)
    assert tested.status_code == 200 and tested.json()["outcome"] == "pass", tested.text
    read = setup.row(admin, pid)["row_version"]  # R: after the test recorded its outcome
    fired = _write_after_read(
        monkeypatch,
        setup.owner_db,
        setup.world.a.tenant,
        pid,
        "config = CAST(:c AS jsonb), last_tested_at = NULL, health_status = 'unknown'",
        {"c": json.dumps({"base_url": evil.base_url, "health_path": "/health"})},
    )
    r = c.post(
        f"{A}/connections/{pid}/status",
        headers=admin,
        json={"status": "active", **_sent(sent, read)},
    )
    assert fired, "the concurrent change did not run between the read and the write"
    monkeypatch.undo()
    assert r.status_code == 409, r.text
    row = setup.row(admin, pid)
    assert row["status"] == "draft" and row["config"]["base_url"] == evil.base_url
    assert evil.requests == []


# ------------------------------------------------------------------ every adapter type
CASES: list[tuple[str, dict[str, Any], str, list[tuple[str, dict[str, Any]]]]] = [
    # adapter, body, auth_method, [(label, config patch)]  -- destination changes
    ("mcp_http", {"connection_type": "mcp", "config": {"url": "http://127.0.0.1:9/mcp"}},
     "bearer", [("url", {"url": "http://127.0.0.1:10/mcp"})]),
    ("openai_compatible",
     {"connection_type": "model_api", "config": {"base_url": "http://127.0.0.1:9/v1"}},
     "bearer", [("base_url", {"base_url": "http://127.0.0.1:10/v1"})]),
    ("anthropic",
     {"connection_type": "model_api", "config": {"base_url": "http://127.0.0.1:9"}},
     "api_key", [("base_url", {"base_url": "http://127.0.0.1:10"})]),
    ("postgres",
     {"connection_type": "database",
      "config": {"host": "db.example.test", "port": 5432, "database": "d", "user": "u"}},
     "basic",
     [("host", {"host": "other.example.test", "port": 5432, "database": "d", "user": "u"}),
      ("port", {"host": "db.example.test", "port": 5433, "database": "d", "user": "u"}),
      ("sslmode", {"host": "db.example.test", "port": 5432, "database": "d", "user": "u",
                   "sslmode": "disable"})]),
    ("databricks", {"connection_type": "data_platform", "config": {"host": "a.example.test"}},
     "bearer", [("host", {"host": "b.example.test"})]),
    ("snowflake", {"connection_type": "data_platform", "config": {"account": "acct1"}},
     "bearer", [("account", {"account": "acct2"})]),
    ("fabric", {"connection_type": "data_platform", "config": {"endpoint": "a.example.test"}},
     "bearer", [("endpoint", {"endpoint": "b.example.test"})]),
]  # fmt: skip


@pytest.mark.parametrize(("adapter", "body", "auth", "changes"), CASES, ids=[c[0] for c in CASES])
def test_every_adapters_destination_fields_are_guarded(
    setup: Setup,
    adapter: str,
    body: dict[str, Any],
    auth: str,
    changes: list[tuple[str, dict[str, Any]]],
) -> None:
    c, A = setup.client, setup.A
    admin = setup.as_admin()
    sid = secret(setup.app_db, setup.settings, setup.world.a.tenant, f"value-for-{adapter}-123")
    created = connection(
        c,
        admin,
        {**body, "name": f"c-{adapter}", "adapter_key": adapter, "auth_method": auth,
         "secret_id": sid},
        A,
    )  # fmt: skip
    hm = setup.as_manager()
    for label, cfg in changes:
        r = setup.patch(hm, created["id"], {"config": cfg})
        assert r.status_code == 403, f"{adapter}.{label}: {r.status_code} {r.text}"
    assert setup.row(hm, created["id"])["config"] == body["config"]
    # the same config, byte for byte, is not a host change (and needs no secret:use)
    same = setup.patch(hm, created["id"], {"config": body["config"], "name": f"n-{adapter}"})
    assert same.status_code == 200, same.text
    # the secret holder (tenant administrator) may change it
    ok = setup.patch(setup.as_admin(), created["id"], {"config": changes[0][1]})
    assert ok.status_code == 200, ok.text


def test_every_adapter_declares_the_config_fields_that_pick_its_destination() -> None:
    """Drift guard: a new adapter with a host-like config property must list it as a destination
    field, or the host-change rule would not see it."""
    hostlike = {"base_url", "url", "host", "hostname", "endpoint", "account", "server", "address"}
    seen = 0
    for desc in AdapterRegistry.load().descriptors():
        props = set((desc.config_schema.get("properties") or {}).keys())
        needed = props & hostlike
        if desc.connection_type is not None and needed:
            seen += 1
            assert needed <= set(desc.destination_fields), desc.key
    assert seen >= 6
