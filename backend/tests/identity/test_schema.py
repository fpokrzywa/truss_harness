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
    ledger_0001 (B-1001), so neither is a version head any more; tenancy_0002..0006 follow it.
    The runtime branch has one head, runtime_0004 (issue #82) after runtime_0003 (B-2512) and
    runtime_0002 (B-2514); runtime_0004 depends on ledger_0003, so ledger_0003 is not a version
    head either; runtime_0005 (issue #33) follows runtime_0004, runtime_0006 (issue #126)
    follows runtime_0005 and runtime_0007 (issue #224) follows runtime_0006, so runtime_0007 is
    the runtime head.
    delivery_0001 depends on tenancy_0004 and identity_0006 (B-2526); tenancy_0005 follows
    tenancy_0004, so tenancy_0004 is not a version head either; tenancy_0006 follows tenancy_0005
    (B-2550). delivery_0002 follows delivery_0001 (B-2664), delivery_0003 follows delivery_0002
    (B-2669), delivery_0004 follows delivery_0003 (B-2675) and delivery_0005 follows delivery_0004
    (B-2681), so the delivery head is delivery_0005. packaging_0001 depends on build_0002,
    models_admin_0003 and tenancy_0006 (issue #78, B-2701), so none of the three is a version
    head any more; packaging_0002 follows packaging_0001 (issue #79), so packaging_0002 is the
    packaging head. evaluation_0001 depends on packaging_0002 and runtime_0004 (issue #80), so
    neither is a version head any more and evaluation_0001 is the evaluation head.
    delivery_0006 follows delivery_0005 and depends on evaluation_0001 (issue #81), so the
    delivery head is delivery_0006 and evaluation_0001 is not a version head any more.
    delivery_0007 follows delivery_0006 with no depends_on (issue #270), so delivery_0007 is the
    delivery head and delivery_0006 is not a version head any more.
    identity_0007 follows identity_0006 with no depends_on (issue #83), so it is a new version
    head; identity_0006 stays a non-head (delivery_0001 depends on it, and identity_0007 follows
    it). tenancy_0007 follows tenancy_0006 with no depends_on (issue #39), so it is a version
    head. packaging_0003 follows packaging_0002 with no depends_on (issue #158), so it is the
    packaging version head (packaging_0002 stays a non-head). appgen_0001 (issue #255) depends on
    packaging_0003, runtime_0007, tenancy_0007 and identity_0007, so none of the four is a version
    head any more and appgen_0001 is the appgen head. mcp_0001 depends on tenancy_0007 and
    identity_0007 (issue #287) and is the mcp head. assist_0001 (issue #278) depends on
    appgen_0001 (through which it reaches authz_0002, identity_0007, tenancy_0007, build_0002,
    runtime_0007 and models_admin_0004), so appgen_0001 is not a version head any more and
    assist_0001 is the assist head. runtime_0008 (issue #174) follows runtime_0007 with no
    depends_on, so it is a new version head (runtime_0007 stays a non-head). packaging_0004
    follows packaging_0003 with no depends_on (issue #271), so it is the packaging version head
    (packaging_0003 stays a non-head)."""
    heads = code_heads()
    assert "assist_0001" in heads
    assert "appgen_0001" not in heads
    assert "models_admin_0004" not in heads
    assert "mcp_0001" in heads
    assert "identity_0007" not in heads
    assert "identity_0006" not in heads
    assert "delivery_0007" in heads
    assert "delivery_0006" not in heads
    assert "evaluation_0001" not in heads
    assert "delivery_0005" not in heads
    assert "delivery_0004" not in heads
    assert "delivery_0003" not in heads
    assert "delivery_0002" not in heads
    assert "delivery_0001" not in heads
    assert "runtime_0008" in heads
    assert "runtime_0007" not in heads
    assert "runtime_0006" not in heads
    assert "runtime_0005" not in heads
    assert "runtime_0004" not in heads
    assert "ledger_0003" not in heads
    assert "runtime_0003" not in heads
    assert "runtime_0002" not in heads
    assert "runtime_0001" not in heads
    assert "packaging_0004" in heads
    assert "packaging_0003" not in heads
    assert "packaging_0002" not in heads
    assert "packaging_0001" not in heads
    assert "build_0002" not in heads
    assert "models_admin_0003" not in heads
    assert "tenancy_0007" not in heads
    assert "tenancy_0006" not in heads
    assert "tenancy_0005" not in heads
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
