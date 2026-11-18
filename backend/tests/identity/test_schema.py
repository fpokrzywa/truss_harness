"""Identity schema rules. `test_user_identities_must_be_bound_to_an_idp` is SETH 3ba0e06's
`tests/test_p3_adapters.py` test, moved unchanged in assertion (its `world` is identity's)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.migrations import code_heads


def test_user_identities_must_be_bound_to_an_idp(world: object, owner_session: Session) -> None:
    """P2 review minor: `idp_config_id` is NOT NULL (migration p03_0002)."""
    nullable = owner_session.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'shifde' "
            "AND table_name = 'user_identities' AND column_name = 'idp_config_id'"
        )
    ).scalar_one()
    assert nullable == "NO"
    uid = owner_session.execute(text("SELECT id FROM shifde.users LIMIT 1")).scalar_one()
    with pytest.raises(DBAPIError, match="not-null|null value"):
        owner_session.execute(
            text(
                "INSERT INTO shifde.user_identities (id, user_id, issuer, subject) "
                "VALUES (gen_random_uuid(), :u, 'https://x.test', 's')"
            ),
            {"u": uid},
        )
    owner_session.rollback()


def test_a_head_named_in_depends_on_is_not_a_version_head() -> None:
    """identity_0001 depends on secrets_0001 (a branch head): Alembic keeps only identity_0001 in
    `alembic_version`, and `code_heads` (readiness, skeleton tests) agrees (B-906). identity_0005
    depends on authz_0002, the authz head (B-928). tenancy_0001 merges identity_0005 and
    ledger_0001 (B-1001), so neither is a version head any more; tenancy_0002..0004 follow it.
    delivery_0001 depends on tenancy_0004 and identity_0006 (B-2526), so tenancy_0004 is no longer
    a version head either."""
    heads = code_heads()
    assert "delivery_0001" in heads
    assert "tenancy_0004" not in heads
    assert "tenancy_0003" not in heads
    assert "tenancy_0002" not in heads
    assert "tenancy_0001" not in heads
    assert "identity_0005" not in heads
    assert "ledger_0001" not in heads
    assert "identity_0004" not in heads
    assert "authz_0002" not in heads
    assert "identity_0003" not in heads
    assert "identity_0002" not in heads
    assert "identity_0001" not in heads
    assert "secrets_0001" not in heads
