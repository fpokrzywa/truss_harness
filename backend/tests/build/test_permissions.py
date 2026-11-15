"""build part 2b: the permission catalog the build area relies on (SETH `test_p4_governance.py`,
moved with its assertions unchanged; it reads only the seeded `permissions` and role tables)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.build.conftest import owner_session

__all__ = ["owner_session"]


def test_contract_permissions_exist_and_roles_hold_them(owner_session: Session) -> None:
    wanted = {
        "prompt:read",
        "prompt:create",
        "prompt:update",
        "prompt:publish",
        "prompt:test",
        "agent:read",
        "agent:create",
        "agent:update",
        "agent:publish",
        "agent:test",
        "agent:activate",
        "agent:execute",
        "tool:read",
        "tool:create",
        "tool:update",
        "tool:publish",
        "tool:test",
        "tool:test_side_effects",
        "tool:invoke",
        "skill:read",
        "skill:create",
        "skill:update",
        "skill:publish",
        "approval:decide",
        "model:invoke",
        "connection:use",
    }
    have = {
        r[0]: r[1]
        for r in owner_session.execute(text("SELECT key, is_privileged FROM shifde.permissions"))
    }
    assert wanted <= set(have)
    assert have["tool:test_side_effects"] is True and have["agent:activate"] is False

    def role_keys(role: str) -> set[str]:
        return set(
            owner_session.execute(
                text(
                    "SELECT permission_key FROM shifde.role_permissions rp "
                    "JOIN shifde.roles r ON r.id = rp.role_id WHERE r.key = :k"
                ),
                {"k": role},
            ).scalars()
        )

    assert wanted <= role_keys("tenant_administrator") - {"tool:invoke", "agent:execute"} | wanted
    # the same intent, parenthesised so that it can fail (above, `-` binds before `|` and the
    # right-hand `| wanted` makes it always true): the administrator holds every wanted key
    # except possibly the two named
    assert wanted - {"tool:invoke", "agent:execute"} <= role_keys("tenant_administrator")
    for admin in ("tenant_administrator", "workspace_administrator", "project_administrator"):
        assert {"tool:create", "tool:test_side_effects", "agent:activate"} <= role_keys(admin), (
            admin
        )
    dev = role_keys("developer")
    assert {"tool:create", "tool:update", "tool:publish", "tool:test", "prompt:publish"} <= dev
    assert not dev & {"tool:test_side_effects", "agent:activate", "approval:decide"}
    assert not role_keys("viewer") & wanted - {
        "prompt:read",
        "agent:read",
        "tool:read",
        "skill:read",
    }
    assert not role_keys("operator") & {"tool:create", "tool:test_side_effects", "agent:activate"}
    assert all(k.endswith(":read") for k in role_keys("viewer"))
