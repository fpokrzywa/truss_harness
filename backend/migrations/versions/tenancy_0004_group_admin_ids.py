"""Tenancy: platform administrators reaching a client through a group (admin isolation review 1).

Revision ID: tenancy_0004
Revises: tenancy_0003

* Admin isolation review 1, finding 1 (B-1407): a platform administrator who is a member of a
  platform group reaches every client where the group is bound, so each binding that takes
  effect must be a `tenancy.platform_admin.self_added` record there for each such administrator
  (B-1012). New definer `shifde_bound_group_admin_ids(uuid)`: the ids of the platform
  administrators who are members of `p_group`, only when `p_group` holds a role binding in
  `current_tenant_id()`; nothing without a client or for a group not bound there. The client
  context reads no `group_members` or other person's `users` row, so the owner's binding path
  needs it.

Rollback: drops the definer.
"""

from __future__ import annotations

from alembic import op

revision = "tenancy_0004"
down_revision = "tenancy_0003"
branch_labels = None
depends_on = None

_LOCKED = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"

BOUND_GROUP_ADMIN_IDS = f"""CREATE FUNCTION shifde.shifde_bound_group_admin_ids(p_group uuid)
   RETURNS SETOF uuid
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT gm.user_id FROM shifde.group_members gm JOIN shifde.users u ON u.id = gm.user_id
       WHERE gm.group_id = p_group AND u.is_platform_admin
         AND shifde.current_tenant_id() IS NOT NULL
         AND EXISTS (SELECT 1 FROM shifde.role_bindings rb
                      WHERE rb.tenant_id = shifde.current_tenant_id()
                        AND rb.subject_type = 'group' AND rb.subject_id = p_group)
       ORDER BY gm.user_id $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        BOUND_GROUP_ADMIN_IDS,
        "REVOKE ALL ON FUNCTION shifde.shifde_bound_group_admin_ids(uuid) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION shifde.shifde_bound_group_admin_ids(uuid) TO shifde_app",
    )


def downgrade() -> None:
    _run("DROP FUNCTION shifde.shifde_bound_group_admin_ids(uuid)")
