"""Regression tests for the fixes the vendors port made (DECISIONS B-1302, B-1304)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters import evidence
from shifde.audit import service as audit
from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.vendors import service as vendors
from tests.identity.harness import login
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People
from tests.vendors.conftest import vendors_client

LEAK = 'relation "acme_payroll.salaries" does not exist; SELECT ssn FROM acme_payroll.salaries'


def _run_with_detail(owner: Session) -> evidence.ConformanceRun:
    run = evidence.ConformanceRun(
        adapter_key="postgres",
        adapter_version="1.0.0",
        suite_version="1",
        started_at=utcnow(),
        finished_at=utcnow(),
        outcome="fail",
        checks=[
            {
                "name": "discover_schema",
                "status": "fail",
                "capabilities": ["discover.schema"],
                "detail": LEAK,
            }
        ],
    )
    evidence.record_run(owner, run)
    owner.commit()
    return run


def test_conformance_checks_do_not_carry_detail_to_a_client(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    """A suite pointed at a client's database stores error text naming its tables and SQL in
    `checks.detail`; the route showed it to every client (carry_forward, adapters review 1)."""
    owner_session.execute(text("TRUNCATE shifde.adapter_conformance_runs"))
    owner_session.commit()
    run = _run_with_detail(owner_session)
    try:
        for email in (people.viewer_a, people.owner_b, people.platform):
            with vendors_client(settings) as c:
                login(c, email)
                r = c.get("/api/v1/adapters/postgres/conformance-runs")
                assert r.status_code == 200, (email, r.text)
                (item,) = r.json()["items"]
                assert item["id"] == str(run.id)
                assert item["checks"] == [
                    {
                        "name": "discover_schema",
                        "status": "fail",
                        "capabilities": ["discover.schema"],
                    }
                ]
                assert "acme_payroll" not in r.text and "salaries" not in r.text
        stored = owner_session.execute(
            text("SELECT checks FROM shifde.adapter_conformance_runs WHERE id = :i"),
            {"i": run.id},
        ).scalar_one()
        assert stored[0]["detail"] == LEAK  # the owner's evidence keeps it
    finally:
        owner_session.execute(text("TRUNCATE shifde.adapter_conformance_runs"))
        owner_session.commit()


def test_client_checks_keep_only_the_allowlisted_keys() -> None:
    out = vendors.client_checks(
        [{"name": "n", "status": "pass", "capabilities": [], "detail": "x", "extra": 1}]
    )
    assert out == [{"name": "n", "status": "pass", "capabilities": []}]


def test_global_catalog_changes_are_audited_as_installation_events(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    with vendors_client(settings) as c:
        h = login(c, people.platform)
        r = c.post(
            "/api/v1/platform/vendors",
            json={"key": "audited-global", "name": "G", "category": "other"},
            headers=h,
        )
        assert r.status_code == 201, r.text
        patched = c.patch(
            f"/api/v1/platform/vendors/{r.json()['id']}", json={"status": "retired"}, headers=h
        )
        assert patched.status_code == 200
    rows = owner_session.execute(
        text(
            "SELECT action, tenant_id FROM shifde.audit_events WHERE target_id = :i "
            "ORDER BY occurred_at"
        ),
        {"i": r.json()["id"]},
    ).all()
    assert [(x.action, x.tenant_id) for x in rows] == [
        ("vendor.created", None),
        ("vendor.updated", None),
    ]


def test_a_client_context_cannot_file_an_installation_vendor_event(
    world: World, app_db: Database
) -> None:
    """The two vendor actions are installation events only under the platform-administrator
    context; a client user cannot use them to write an event with no client."""
    with app_session(app_db, world.a.tenant) as s, pytest.raises(audit.NotInstallationAction):
        audit.record(s, RequestContext(request_id="t"), action="vendor.created", tenant_id=None)
