"""Identity, part 3b: SHI-wide groups, part 1 (groups become platform-level, design 5.3).

Revision ID: identity_0005
Revises: identity_0004
Depends on: authz_0002

A group (say "Databricks FDEs") exists once, at platform level, and holds staff; it reaches a
client only through role bindings stored in that client (DECISIONS B-928..B-935):

* `groups` and `group_members` lose `tenant_id` (SETH 3ba0e06 keyed every group to one client).
  `groups.slug` is unique platform-wide; IdP-mapped groups are unique per (IdP, external name).
  `group_members.group_id -> groups (id)` and `group_members.user_id -> users (id)` replace the
  composite client keys; the deferred `(tenant_id, user_id) -> memberships` key is dropped for
  good (a staff member need not be a member of any client to be in a group).
* Row-level security: writes only by the platform administrator (`platform_admin_all`); a staff
  member reads the groups they belong to and their own membership rows, only outside any client
  context (`member_read`, `own_row`), so code running inside a client never learns the names of
  groups bound elsewhere. A client context reads no row of either table.
* `shifde_bound_groups()` (zero-argument definer): id and name of the groups bound in the
  current client, nothing else (no members, slug, description, IdP mapping).
* `shifde_active_group_ids(uuid)` (definer): the person's active groups that hold a role binding
  in the current client (the `authz.GroupMembership` the PDP uses); empty without a client.
* `shifde_sync_idp_groups(uuid, uuid, text[])` (definer): IdP group sync for an active
  installation IdP with `group_sync_mode = 'sync'`, for a person with an identity at that IdP,
  into groups the platform administrator mapped to that IdP; it never creates a group and
  refuses inside any client context (B-936: code running in client A must not enrol a person
  into a group bound in B).
* `check_role_binding()`: the group branch requires a platform group and the platform-
  administrator context (who may grant: the client-owner path is tenancy's); the scope branches
  are nested so a tenant-scope binding no longer plans the `workspaces`/`projects` queries
  (they still fail closed until tenancy creates those tables).
* A group's IdP is an installation IdP only (`trg_groups_idp_scope`; `trg_idp_configs_group_scope`
  refuses moving a mapped IdP into a client) and a client's IdP never syncs groups
  (`ck_idp_configs__group_sync_installation_only`).

Rollback: restores SETH's per-client columns, keys, policies and trigger body. Platform groups
cannot be mapped back to one client, so every group, group member and group role binding is
deleted.
"""

from __future__ import annotations

from alembic import op

revision = "identity_0005"
down_revision = "identity_0004"
branch_labels = None
depends_on = ("authz_0002",)

_LOCKED = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
_BOUND_IN_CLIENT = (
    "EXISTS (SELECT 1 FROM shifde.role_bindings rb "
    "WHERE rb.tenant_id = shifde.current_tenant_id() AND rb.subject_type = 'group' "
    "AND rb.subject_id = g.id)"
)

FUNCTIONS: dict[str, str] = {
    "shifde_bound_groups()": f"""CREATE FUNCTION shifde.shifde_bound_groups()
   RETURNS TABLE (id uuid, name text)
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT g.id, g.name FROM shifde.groups g
       WHERE shifde.current_tenant_id() IS NOT NULL AND {_BOUND_IN_CLIENT}
       ORDER BY g.name, g.id $$""",
    "shifde_active_group_ids(uuid)": f"""CREATE FUNCTION shifde.shifde_active_group_ids(
       p_user uuid)
   RETURNS SETOF uuid
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT g.id FROM shifde.group_members gm JOIN shifde.groups g ON g.id = gm.group_id
       WHERE gm.user_id = p_user AND g.status = 'active'
         AND shifde.current_tenant_id() IS NOT NULL AND {_BOUND_IN_CLIENT} $$""",
    "shifde_sync_idp_groups(uuid, uuid, text[])": f"""CREATE FUNCTION
       shifde.shifde_sync_idp_groups(p_idp uuid, p_user uuid, p_names text[])
   RETURNS integer
   LANGUAGE plpgsql VOLATILE {_LOCKED} AS
   $$ DECLARE v_count integer;
      BEGIN
        IF shifde.current_tenant_id() IS NOT NULL
           OR p_names IS NULL OR pg_catalog.cardinality(p_names) > 200
           OR NOT EXISTS (SELECT 1 FROM shifde.idp_configs c
                          WHERE c.id = p_idp AND c.tenant_id IS NULL AND c.status = 'active'
                            AND c.group_sync_mode = 'sync')
           OR NOT EXISTS (SELECT 1 FROM shifde.user_identities i
                          WHERE i.idp_config_id = p_idp AND i.user_id = p_user) THEN
          RAISE EXCEPTION 'group sync refused' USING ERRCODE = 'insufficient_privilege';
        END IF;
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
      END $$""",
}

_CHECK_ROLE_BINDING = """CREATE OR REPLACE FUNCTION shifde.check_role_binding() RETURNS trigger
   LANGUAGE plpgsql AS
   $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM shifde.roles r WHERE r.id = NEW.role_id
                       AND (r.tenant_id IS NULL OR r.tenant_id = NEW.tenant_id)) THEN
          RAISE EXCEPTION 'role is not available to this tenant'
            USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.subject_type = 'user' THEN
          IF NOT EXISTS (SELECT 1 FROM shifde.memberships m
                         WHERE m.tenant_id = NEW.tenant_id AND m.user_id = NEW.subject_id) THEN
            RAISE EXCEPTION 'subject user is not a member of this tenant'
              USING ERRCODE = 'check_violation';
          END IF;
        ELSIF NEW.subject_type = 'group' THEN
          IF NOT shifde.is_platform_admin_ctx() THEN
            RAISE EXCEPTION 'binding a group needs the platform administrator'
              USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM shifde.groups g WHERE g.id = NEW.subject_id) THEN
            RAISE EXCEPTION 'subject group is not a platform group'
              USING ERRCODE = 'check_violation';
          END IF;
        ELSIF NEW.subject_type = 'service_account' THEN
          IF NOT EXISTS (SELECT 1 FROM shifde.service_accounts s
                         WHERE s.tenant_id = NEW.tenant_id AND s.id = NEW.subject_id) THEN
            RAISE EXCEPTION 'subject service account is not in this tenant'
              USING ERRCODE = 'check_violation';
          END IF;
        END IF;
        IF NEW.scope_type = 'workspace' THEN
          IF NOT EXISTS (SELECT 1 FROM shifde.workspaces w
                         WHERE w.tenant_id = NEW.tenant_id AND w.id = NEW.scope_id) THEN
            RAISE EXCEPTION 'scope workspace is not in this tenant'
              USING ERRCODE = 'check_violation';
          END IF;
        ELSIF NEW.scope_type = 'project' THEN
          IF NOT EXISTS (SELECT 1 FROM shifde.projects p
                         WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.scope_id) THEN
            RAISE EXCEPTION 'scope project is not in this tenant'
              USING ERRCODE = 'check_violation';
          END IF;
        ELSIF NEW.scope_type = 'resource' AND NEW.resource_type IS NULL THEN
          RAISE EXCEPTION 'resource bindings need a resource_type'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
      END $$"""

# authz_0001's body, verbatim (SETH 3ba0e06), for the downgrade.
_SETH_CHECK_ROLE_BINDING = """CREATE OR REPLACE FUNCTION shifde.check_role_binding()
   RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM shifde.roles r WHERE r.id = NEW.role_id
                               AND (r.tenant_id IS NULL OR r.tenant_id = NEW.tenant_id)) THEN
                  RAISE EXCEPTION 'role is not available to this tenant'
                    USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.subject_type = 'user' AND NOT EXISTS (
                     SELECT 1 FROM shifde.memberships m
                     WHERE m.tenant_id = NEW.tenant_id AND m.user_id = NEW.subject_id) THEN
                  RAISE EXCEPTION 'subject user is not a member of this tenant'
                    USING ERRCODE = 'check_violation';
                ELSIF NEW.subject_type = 'group' AND NOT EXISTS (
                     SELECT 1 FROM shifde.groups g
                     WHERE g.tenant_id = NEW.tenant_id AND g.id = NEW.subject_id) THEN
                  RAISE EXCEPTION 'subject group is not in this tenant'
                    USING ERRCODE = 'check_violation';
                ELSIF NEW.subject_type = 'service_account' AND NOT EXISTS (
                     SELECT 1 FROM shifde.service_accounts s
                     WHERE s.tenant_id = NEW.tenant_id AND s.id = NEW.subject_id) THEN
                  RAISE EXCEPTION 'subject service account is not in this tenant'
                    USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.scope_type = 'workspace' AND NOT EXISTS (
                     SELECT 1 FROM shifde.workspaces w
                     WHERE w.tenant_id = NEW.tenant_id AND w.id = NEW.scope_id) THEN
                  RAISE EXCEPTION 'scope workspace is not in this tenant'
                    USING ERRCODE = 'check_violation';
                ELSIF NEW.scope_type = 'project' AND NOT EXISTS (
                     SELECT 1 FROM shifde.projects p
                     WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.scope_id) THEN
                  RAISE EXCEPTION 'scope project is not in this tenant'
                    USING ERRCODE = 'check_violation';
                ELSIF NEW.scope_type = 'resource' AND NEW.resource_type IS NULL THEN
                  RAISE EXCEPTION 'resource bindings need a resource_type'
                    USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
              END $$"""

_GROUP_IDP_SCOPE = """CREATE FUNCTION shifde.check_group_idp_scope() RETURNS trigger
   LANGUAGE plpgsql AS
   $$ BEGIN
        IF NEW.idp_config_id IS NOT NULL AND NOT EXISTS (
             SELECT 1 FROM shifde.idp_configs c
              WHERE c.id = NEW.idp_config_id AND c.tenant_id IS NULL) THEN
          RAISE EXCEPTION 'a group is synced only from an installation identity provider'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
      END $$"""

_IDP_GROUP_SCOPE = """CREATE FUNCTION shifde.check_idp_group_scope() RETURNS trigger
   LANGUAGE plpgsql AS
   $$ BEGIN
        IF NEW.tenant_id IS NOT NULL AND EXISTS (
             SELECT 1 FROM shifde.groups g WHERE g.idp_config_id = NEW.id) THEN
          RAISE EXCEPTION 'an identity provider mapped to platform groups stays installation-wide'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
      END $$"""

_PLATFORM_ADMIN = "shifde.is_platform_admin_ctx()"


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        # SETH's per-client groups cannot be given a platform meaning: start empty
        "DELETE FROM shifde.role_bindings WHERE subject_type = 'group'",
        "DELETE FROM shifde.group_members",
        "DELETE FROM shifde.groups",
        "DROP POLICY tenant_isolation ON shifde.group_members",
        "DROP POLICY tenant_isolation ON shifde.groups",
        "ALTER TABLE shifde.group_members DROP CONSTRAINT fk_group_members__group",
        "DROP INDEX shifde.ix_group_members__user",
        "DROP INDEX shifde.uq_groups__idp_external",
        "ALTER TABLE shifde.groups DROP CONSTRAINT uq_groups__tenant_id_slug",
        "ALTER TABLE shifde.groups DROP CONSTRAINT uq_groups__tenant_id_id",
        "ALTER TABLE shifde.group_members DROP COLUMN tenant_id",
        "ALTER TABLE shifde.groups DROP COLUMN tenant_id",
        "ALTER TABLE shifde.groups ADD CONSTRAINT uq_groups__slug UNIQUE (slug)",
        "CREATE UNIQUE INDEX uq_groups__idp_external ON shifde.groups "
        "(idp_config_id, external_id) WHERE source = 'idp'",
        "ALTER TABLE shifde.group_members ADD CONSTRAINT fk_group_members__group "
        "FOREIGN KEY (group_id) REFERENCES shifde.groups (id)",
        "ALTER TABLE shifde.group_members ADD CONSTRAINT fk_group_members__user "
        "FOREIGN KEY (user_id) REFERENCES shifde.users (id)",
        "CREATE INDEX ix_group_members__user ON shifde.group_members (user_id)",
        # writes: the platform administrator only (a platform catalog, B-116 item 10)
        f"CREATE POLICY platform_admin_all ON shifde.groups USING ({_PLATFORM_ADMIN}) "
        f"WITH CHECK ({_PLATFORM_ADMIN})",
        f"CREATE POLICY platform_admin_all ON shifde.group_members USING ({_PLATFORM_ADMIN}) "
        f"WITH CHECK ({_PLATFORM_ADMIN})",
        # reads: a staff member's own groups and membership rows, outside any client context
        "CREATE POLICY member_read ON shifde.groups FOR SELECT USING "
        "(shifde.current_tenant_id() IS NULL AND EXISTS (SELECT 1 FROM shifde.group_members gm "
        "WHERE gm.group_id = groups.id AND gm.user_id = shifde.current_principal_id()))",
        "CREATE POLICY own_row ON shifde.group_members FOR SELECT USING "
        "(user_id = shifde.current_principal_id() AND shifde.current_tenant_id() IS NULL)",
        # a group's IdP is an installation IdP; a client's IdP never syncs groups
        _GROUP_IDP_SCOPE,
        "CREATE TRIGGER trg_groups_idp_scope BEFORE INSERT OR UPDATE OF idp_config_id "
        "ON shifde.groups FOR EACH ROW EXECUTE FUNCTION shifde.check_group_idp_scope()",
        _IDP_GROUP_SCOPE,
        "CREATE TRIGGER trg_idp_configs_group_scope BEFORE UPDATE OF tenant_id "
        "ON shifde.idp_configs FOR EACH ROW EXECUTE FUNCTION shifde.check_idp_group_scope()",
        "ALTER TABLE shifde.idp_configs ADD CONSTRAINT ck_idp_configs__group_sync_installation_only "
        "CHECK (tenant_id IS NULL OR group_sync_mode = 'off')",
        _CHECK_ROLE_BINDING,
    )
    for signature, ddl in FUNCTIONS.items():
        _run(
            ddl,
            f"REVOKE ALL ON FUNCTION shifde.{signature} FROM PUBLIC",
            f"GRANT EXECUTE ON FUNCTION shifde.{signature} TO shifde_app",
        )


def downgrade() -> None:
    for signature in reversed(FUNCTIONS):
        _run(f"DROP FUNCTION IF EXISTS shifde.{signature}")
    _run(
        _SETH_CHECK_ROLE_BINDING,
        "ALTER TABLE shifde.idp_configs DROP CONSTRAINT "
        "ck_idp_configs__group_sync_installation_only",
        "DROP TRIGGER trg_idp_configs_group_scope ON shifde.idp_configs",
        "DROP FUNCTION shifde.check_idp_group_scope()",
        "DROP TRIGGER trg_groups_idp_scope ON shifde.groups",
        "DROP FUNCTION shifde.check_group_idp_scope()",
        "DELETE FROM shifde.role_bindings WHERE subject_type = 'group'",
        "DELETE FROM shifde.group_members",
        "DELETE FROM shifde.groups",
        "DROP POLICY own_row ON shifde.group_members",
        "DROP POLICY member_read ON shifde.groups",
        "DROP POLICY platform_admin_all ON shifde.group_members",
        "DROP POLICY platform_admin_all ON shifde.groups",
        "DROP INDEX shifde.ix_group_members__user",
        "ALTER TABLE shifde.group_members DROP CONSTRAINT fk_group_members__user",
        "ALTER TABLE shifde.group_members DROP CONSTRAINT fk_group_members__group",
        "DROP INDEX shifde.uq_groups__idp_external",
        "ALTER TABLE shifde.groups DROP CONSTRAINT uq_groups__slug",
        "ALTER TABLE shifde.groups ADD COLUMN tenant_id uuid NOT NULL",
        "ALTER TABLE shifde.group_members ADD COLUMN tenant_id uuid NOT NULL",
        "ALTER TABLE shifde.groups ADD CONSTRAINT uq_groups__tenant_id_slug UNIQUE (tenant_id, slug)",
        "ALTER TABLE shifde.groups ADD CONSTRAINT uq_groups__tenant_id_id UNIQUE (tenant_id, id)",
        "CREATE UNIQUE INDEX uq_groups__idp_external ON shifde.groups "
        "(tenant_id, idp_config_id, external_id) WHERE source = 'idp'",
        "ALTER TABLE shifde.group_members ADD CONSTRAINT fk_group_members__group "
        "FOREIGN KEY (tenant_id, group_id) REFERENCES shifde.groups (tenant_id, id)",
        "CREATE INDEX ix_group_members__user ON shifde.group_members (tenant_id, user_id)",
        "CREATE POLICY tenant_isolation ON shifde.groups "
        "USING (tenant_id = shifde.current_tenant_id()) "
        "WITH CHECK (tenant_id = shifde.current_tenant_id())",
        "CREATE POLICY tenant_isolation ON shifde.group_members "
        "USING (tenant_id = shifde.current_tenant_id()) "
        "WITH CHECK (tenant_id = shifde.current_tenant_id())",
    )
