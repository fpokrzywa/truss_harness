"""Identity: IdP group sync reports what it changed (admin part 3a, admin review 2 item 1).

Revision ID: identity_0006
Revises: identity_0005

* `shifde_sync_idp_groups(uuid, uuid, text[])` (definer, same signature, same refusals as
  identity_0005) now returns one row per group the person is in or just left through this IdP:
  `synced_group_id`, `change` (`added`, `removed` or `kept`) and, for an added or removed group
  only, `bound_tenant_ids`, the clients where that group holds a role binding. Sign-in has no
  client context and no platform-administrator flag, so it cannot otherwise find the clients
  whose audit log must record the change and a platform administrator's self-add there
  (B-1403, B-1407, B-1408; DECISIONS B-1624). The count identity_0005 returned is the number of
  `added` and `kept` rows.

Rollback: restores the identity_0005 function (returns the count).
"""

from __future__ import annotations

from alembic import op

revision = "identity_0006"
down_revision = "identity_0005"
branch_labels = None
depends_on = None

_LOCKED = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
_SIG = "shifde.shifde_sync_idp_groups(uuid, uuid, text[])"
_REFUSE = """IF shifde.current_tenant_id() IS NOT NULL
           OR p_names IS NULL OR pg_catalog.cardinality(p_names) > 200
           OR NOT EXISTS (SELECT 1 FROM shifde.idp_configs c
                          WHERE c.id = p_idp AND c.tenant_id IS NULL AND c.status = 'active'
                            AND c.group_sync_mode = 'sync')
           OR NOT EXISTS (SELECT 1 FROM shifde.user_identities i
                          WHERE i.idp_config_id = p_idp AND i.user_id = p_user) THEN
          RAISE EXCEPTION 'group sync refused' USING ERRCODE = 'insufficient_privilege';
        END IF;"""

NEW = f"""CREATE FUNCTION shifde.shifde_sync_idp_groups(p_idp uuid, p_user uuid, p_names text[])
   RETURNS TABLE (synced_group_id uuid, change text, bound_tenant_ids uuid[])
   LANGUAGE plpgsql VOLATILE {_LOCKED} AS
   $$ DECLARE v_removed uuid[]; v_added uuid[];
      BEGIN
        {_REFUSE}
        WITH d AS (
          DELETE FROM shifde.group_members gm USING shifde.groups g
           WHERE g.id = gm.group_id AND gm.user_id = p_user AND gm.source = 'idp_sync'
             AND g.source = 'idp' AND g.idp_config_id = p_idp
             AND NOT (g.external_id = ANY (p_names))
          RETURNING gm.group_id AS gid)
        SELECT COALESCE(pg_catalog.array_agg(d.gid), '{{}}') INTO v_removed FROM d;
        WITH a AS (
          INSERT INTO shifde.group_members (group_id, user_id, source, added_at)
          SELECT g.id, p_user, 'idp_sync', pg_catalog.now() FROM shifde.groups g
           WHERE g.source = 'idp' AND g.idp_config_id = p_idp AND g.external_id = ANY (p_names)
          ON CONFLICT (group_id, user_id) DO NOTHING
          RETURNING group_members.group_id AS gid)
        SELECT COALESCE(pg_catalog.array_agg(a.gid), '{{}}') INTO v_added FROM a;
        RETURN QUERY
        SELECT g.id,
               CASE WHEN g.id = ANY (v_added) THEN 'added'
                    WHEN g.id = ANY (v_removed) THEN 'removed' ELSE 'kept' END,
               CASE WHEN g.id = ANY (v_added) OR g.id = ANY (v_removed) THEN
                 ARRAY(SELECT DISTINCT rb.tenant_id FROM shifde.role_bindings rb
                        WHERE rb.subject_type = 'group' AND rb.subject_id = g.id
                        ORDER BY rb.tenant_id)
               ELSE '{{}}'::uuid[] END
          FROM shifde.groups g
         WHERE g.idp_config_id = p_idp
           AND (g.id = ANY (v_removed) OR g.id = ANY (v_added)
                OR EXISTS (SELECT 1 FROM shifde.group_members gm
                            WHERE gm.group_id = g.id AND gm.user_id = p_user
                              AND gm.source = 'idp_sync'))
         ORDER BY g.id;
      END $$"""

OLD = f"""CREATE FUNCTION shifde.shifde_sync_idp_groups(p_idp uuid, p_user uuid, p_names text[])
   RETURNS integer
   LANGUAGE plpgsql VOLATILE {_LOCKED} AS
   $$ DECLARE v_count integer;
      BEGIN
        {_REFUSE}
        DELETE FROM shifde.group_members gm USING shifde.groups g
         WHERE g.id = gm.group_id AND gm.user_id = p_user AND gm.source = 'idp_sync'
           AND g.source = 'idp' AND g.idp_config_id = p_idp
           AND NOT (g.external_id = ANY (p_names));
        INSERT INTO shifde.group_members (group_id, user_id, source, added_at)
        SELECT g.id, p_user, 'idp_sync', pg_catalog.now() FROM shifde.groups g
         WHERE g.source = 'idp' AND g.idp_config_id = p_idp AND g.external_id = ANY (p_names)
        ON CONFLICT (group_id, user_id) DO NOTHING;
        SELECT pg_catalog.count(*) INTO v_count
          FROM shifde.group_members gm JOIN shifde.groups g ON g.id = gm.group_id
         WHERE gm.user_id = p_user AND gm.source = 'idp_sync' AND g.idp_config_id = p_idp;
        RETURN v_count;
      END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _replace(body: str) -> None:
    _run(
        f"DROP FUNCTION {_SIG}",
        body,
        f"REVOKE ALL ON FUNCTION {_SIG} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {_SIG} TO shifde_app",
    )


def upgrade() -> None:
    _replace(NEW)


def downgrade() -> None:
    _replace(OLD)
