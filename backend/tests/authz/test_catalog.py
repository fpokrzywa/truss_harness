"""Seeded permission catalog and system role templates.

`test_seeded_catalog_and_templates` is SETH 3ba0e06 `tests/test_rls.py`'s, unchanged. The others
pin that authz_0001 seeds SETH's final catalog (counts replayed from SETH's migrations, over the
keys outside `delivery:%`, `packaging:%` and `evaluation:%`), that delivery_0001 adds exactly its
nine `delivery:*` keys and fifteen template grants (B-2501), that packaging_0001 adds exactly its
eight `packaging:*` and `evaluation:*` keys and fifteen template grants (issue #78), and that
`permissions` is read-only for the application role.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.authz import service as authz
from shifde.core.db import Database


def test_seeded_catalog_and_templates(owner_session: Session) -> None:
    roles = (
        owner_session.execute(
            text("SELECT name FROM shifde.roles WHERE tenant_id IS NULL ORDER BY name")
        )
        .scalars()
        .all()
    )
    assert roles == [
        "Auditor",
        "Developer",
        "Operator",
        "Platform Administrator",
        "Project Administrator",
        "Tenant Administrator",
        "Viewer",
        "Workspace Administrator",
    ]
    viewer_perms = set(
        owner_session.execute(
            text(
                "SELECT permission_key FROM shifde.role_permissions rp "
                "JOIN shifde.roles r ON r.id = rp.role_id WHERE r.key = 'viewer'"
            )
        ).scalars()
    )
    assert {"workspace:read", "project:read", "agent:read", "vendor:read"} <= viewer_perms
    assert all(p.endswith(":read") for p in viewer_perms)  # Viewer is read-only (FR-AUTHZ-008)
    assert not viewer_perms & {"member:read", "audit:read", "secret:read", "apikey:read"}


def test_catalog_is_seths_final_catalog(owner_session: Session) -> None:
    counts = owner_session.execute(
        text(
            "SELECT (SELECT count(*) FROM shifde.permissions WHERE key NOT LIKE 'delivery:%' "
            "AND key NOT LIKE 'packaging:%' AND key NOT LIKE 'evaluation:%' "
            "AND key NOT LIKE 'appgen:%' AND key NOT LIKE 'mcp:%'), "
            "(SELECT count(*) FROM shifde.roles WHERE is_system), "
            "(SELECT count(*) FROM shifde.role_permissions WHERE tenant_id IS NULL "
            "AND permission_key NOT LIKE 'delivery:%' AND permission_key NOT LIKE 'packaging:%' "
            "AND permission_key NOT LIKE 'evaluation:%' AND permission_key NOT LIKE 'appgen:%' "
            "AND permission_key NOT LIKE 'mcp:%')"
        )
    ).one()
    assert tuple(counts) == (92, 8, 339)
    delivery = owner_session.execute(
        text(
            "SELECT (SELECT count(*) FROM shifde.permissions WHERE key LIKE 'delivery:%'), "
            "(SELECT count(*) FROM shifde.role_permissions WHERE tenant_id IS NULL "
            "AND permission_key LIKE 'delivery:%')"
        )
    ).one()
    assert tuple(delivery) == (9, 15)
    packaging = owner_session.execute(
        text(
            "SELECT (SELECT count(*) FROM shifde.permissions WHERE key LIKE 'packaging:%' "
            "OR key LIKE 'evaluation:%'), "
            "(SELECT count(*) FROM shifde.role_permissions WHERE tenant_id IS NULL "
            "AND (permission_key LIKE 'packaging:%' OR permission_key LIKE 'evaluation:%'))"
        )
    ).one()
    assert tuple(packaging) == (8, 15)
    appgen = owner_session.execute(
        text(
            "SELECT (SELECT count(*) FROM shifde.permissions WHERE key LIKE 'appgen:%' "
            "AND NOT is_privileged), "
            "(SELECT count(*) FROM shifde.role_permissions rp JOIN shifde.roles r "
            "ON r.id = rp.role_id WHERE rp.tenant_id IS NULL AND rp.permission_key LIKE 'appgen:%' "
            "AND r.key IN ('tenant_administrator', 'developer'))"
        )
    ).one()
    assert tuple(appgen) == (3, 6)
    mcp = owner_session.execute(
        text(
            "SELECT (SELECT count(*) FROM shifde.permissions WHERE key LIKE 'mcp:%'), "
            "(SELECT count(*) FROM shifde.role_permissions rp JOIN shifde.roles r "
            "ON r.id = rp.role_id WHERE rp.tenant_id IS NULL AND rp.permission_key LIKE 'mcp:%' "
            "AND r.key IN ('tenant_administrator', 'developer'))"
        )
    ).one()
    assert tuple(mcp) == (3, 5)
    platform = set(
        owner_session.execute(
            text(
                "SELECT rp.permission_key FROM shifde.role_permissions rp JOIN shifde.roles r "
                "ON r.id = rp.role_id WHERE r.key = 'platform_administrator'"
            )
        ).scalars()
    )
    assert platform == {"platform:admin"}


def test_app_reads_the_catalog_in_any_context_but_cannot_write_it(app_db: Database) -> None:
    with app_db.session() as s:  # no tenant set
        assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
        keys = {p.key for p in authz.list_permissions(s)}
        assert {"workspace:read", "platform:admin", "secret:use"} <= keys
        assert "tool:test_side_effects" in authz.privileged_keys(s)
        for sql in (
            "INSERT INTO shifde.permissions (key, resource_type, action, description) "
            "VALUES ('x:y', 'x', 'y', 'z')",
            "UPDATE shifde.permissions SET is_privileged = false",
            "DELETE FROM shifde.permissions",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                s.execute(text(sql))
            s.rollback()
