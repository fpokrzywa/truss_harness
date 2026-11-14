"""Tenancy, part 2a: who may grant, who sees a client, and the session pin.

Revision ID: tenancy_0002
Revises: tenancy_0001

* Review 1 item 1: `tenants` gains a RESTRICTIVE `insert_by_platform_admin` FOR INSERT policy
  (only the platform-administrator context creates a client; no slug squatting, no
  `uq_tenants__slug` existence check from a client context). DECISIONS B-1008.
* Visibility (design 5.3, B-1009): a person sees a client only through a live role binding
  there, held directly (with an active membership) or through an active platform group (no
  membership needed; a membership that is not active hides the client).
  `shifde_my_memberships()` lists exactly those clients; role names come only from that
  client's own roles and the system templates (review 1 item 3).
* `shifde_resolve_session(bytea)`: a session pinned to a client resolves only while the person
  can see that client by the rule above and the client is active (findings Item 12 F-03).
* `shifde_revoke_user_sessions(uuid)`: revokes only the sessions pinned to the current client
  and refuses a platform-administrator target (identity review 2 item 1), one generic error.
* Who may grant (design 5.3, B-1010): `check_role_binding()` refuses the
  `platform_administrator` template in a client, and binds a group for the platform
  administrator or for the owner of that client only (new definer
  `shifde_may_bind_group(uuid, uuid)`: a live direct `tenant_administrator` binding at
  organization scope with an active membership, in the current client only).

Rollback: drops the policy and the new definer and restores the previous function bodies
(identity_0002, identity_0005, tenancy_0001) from those migrations.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "tenancy_0002"
down_revision = "tenancy_0001"
branch_labels = None
depends_on = None

_LOCKED = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
_LIVE = "(rb.expires_at IS NULL OR rb.expires_at > pg_catalog.now())"


def _grant_rows(user: str, tenant: str) -> str:
    """Live bindings that let `user` see `tenant`: a direct user binding with an active
    membership, or a binding of an active platform group of the person when no membership of
    theirs in that client is other than active. Rows: `rb`, `r` (the role)."""
    return f"""shifde.role_bindings rb
          JOIN shifde.roles r ON r.id = rb.role_id
                             AND (r.tenant_id IS NULL OR r.tenant_id = rb.tenant_id)
         WHERE rb.tenant_id = {tenant} AND {_LIVE}
           AND ((rb.subject_type = 'user' AND rb.subject_id = {user}
                 AND EXISTS (SELECT 1 FROM shifde.memberships m
                              WHERE m.tenant_id = rb.tenant_id AND m.user_id = {user}
                                AND m.status = 'active'))
             OR (rb.subject_type = 'group'
                 AND rb.subject_id IN (SELECT gm.group_id
                                         FROM shifde.group_members gm
                                         JOIN shifde.groups g ON g.id = gm.group_id
                                        WHERE gm.user_id = {user} AND g.status = 'active')
                 AND NOT EXISTS (SELECT 1 FROM shifde.memberships m
                                  WHERE m.tenant_id = rb.tenant_id AND m.user_id = {user}
                                    AND m.status <> 'active')))"""


MY_MEMBERSHIPS = f"""CREATE OR REPLACE FUNCTION shifde.shifde_my_memberships()
   RETURNS TABLE (tenant_id uuid, slug text, name text, status text, role_names text[])
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT x.id, x.slug, x.name, x.status, x.role_names
        FROM (SELECT t.id, t.slug, t.name, t.status,
                     ARRAY(SELECT DISTINCT r.name
                             FROM {_grant_rows("shifde.current_principal_id()", "t.id")}
                            ORDER BY 1) AS role_names
                FROM shifde.tenants t) x
       WHERE pg_catalog.cardinality(x.role_names) > 0
       ORDER BY x.slug $$"""

RESOLVE_SESSION = f"""CREATE OR REPLACE FUNCTION shifde.shifde_resolve_session(
       p_session_hash bytea)
   RETURNS TABLE (session_id uuid, user_id uuid, csrf_hash bytea, created_at timestamptz,
                  last_seen_at timestamptz, idle_expires_at timestamptz,
                  absolute_expires_at timestamptz, revoked_at timestamptz,
                  email text, display_name text, is_platform_admin boolean,
                  user_status text, active_tenant_id uuid)
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT s.id, s.user_id, s.csrf_hash, s.created_at, s.last_seen_at, s.idle_expires_at,
             s.absolute_expires_at, s.revoked_at, u.primary_email, u.display_name,
             (u.is_platform_admin AND s.active_tenant_id IS NULL), u.status, s.active_tenant_id
        FROM shifde.sessions s JOIN shifde.users u ON u.id = s.user_id
       WHERE s.session_hash = p_session_hash
         AND (s.active_tenant_id IS NULL OR (
              EXISTS (SELECT 1 FROM shifde.tenants t
                       WHERE t.id = s.active_tenant_id AND t.status = 'active')
              AND EXISTS (SELECT 1 FROM {_grant_rows("s.user_id", "s.active_tenant_id")}))) $$"""

REVOKE_USER_SESSIONS = f"""CREATE OR REPLACE FUNCTION shifde.shifde_revoke_user_sessions(
       p_user_id uuid)
   RETURNS integer LANGUAGE plpgsql VOLATILE {_LOCKED} AS
   $$ DECLARE v_count integer;
      BEGIN
        IF shifde.current_tenant_id() IS NULL
           OR NOT EXISTS (SELECT 1 FROM shifde.memberships m
                           WHERE m.user_id = p_user_id
                             AND m.tenant_id = shifde.current_tenant_id())
           OR EXISTS (SELECT 1 FROM shifde.users u
                       WHERE u.id = p_user_id AND u.is_platform_admin) THEN
          RAISE EXCEPTION 'user is not a member of the current tenant'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        UPDATE shifde.sessions SET revoked_at = pg_catalog.now()
         WHERE user_id = p_user_id AND revoked_at IS NULL
           AND active_tenant_id = shifde.current_tenant_id();
        GET DIAGNOSTICS v_count = ROW_COUNT;
        RETURN v_count;
      END $$"""

# The owner of a client: an active person with an active membership there and a live direct
# binding of the `tenant_administrator` system template at organization scope (B-1010).
MAY_BIND_GROUP = f"""CREATE FUNCTION shifde.shifde_may_bind_group(p_tenant uuid, p_group uuid)
   RETURNS boolean
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT p_tenant IS NOT NULL AND p_tenant = shifde.current_tenant_id()
         AND EXISTS (SELECT 1 FROM shifde.groups g WHERE g.id = p_group AND g.status = 'active')
         AND EXISTS (SELECT 1 FROM shifde.memberships m JOIN shifde.users u ON u.id = m.user_id
                      WHERE m.tenant_id = p_tenant AND m.user_id = shifde.current_principal_id()
                        AND m.status = 'active' AND u.status = 'active')
         AND EXISTS (SELECT 1 FROM shifde.role_bindings rb JOIN shifde.roles r ON r.id = rb.role_id
                      WHERE rb.tenant_id = p_tenant AND rb.subject_type = 'user'
                        AND rb.subject_id = shifde.current_principal_id()
                        AND rb.scope_type = 'tenant' AND rb.scope_id = p_tenant
                        AND r.tenant_id IS NULL AND r.key = 'tenant_administrator'
                        AND {_LIVE}) $$"""

CHECK_ROLE_BINDING = """CREATE OR REPLACE FUNCTION shifde.check_role_binding() RETURNS trigger
   LANGUAGE plpgsql AS
   $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM shifde.roles r WHERE r.id = NEW.role_id
                       AND (r.tenant_id IS NULL OR r.tenant_id = NEW.tenant_id)) THEN
          RAISE EXCEPTION 'role is not available to this tenant'
            USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM shifde.roles r WHERE r.id = NEW.role_id
                   AND r.tenant_id IS NULL AND r.key = 'platform_administrator') THEN
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
          IF shifde.is_platform_admin_ctx() THEN
            IF NOT EXISTS (SELECT 1 FROM shifde.groups g WHERE g.id = NEW.subject_id) THEN
              RAISE EXCEPTION 'subject group is not a platform group'
                USING ERRCODE = 'check_violation';
            END IF;
          ELSIF NOT shifde.shifde_may_bind_group(NEW.tenant_id, NEW.subject_id) THEN
            RAISE EXCEPTION 'binding a group needs the platform administrator'
              USING ERRCODE = 'insufficient_privilege';
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


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _previous(module: str) -> ModuleType:
    """An earlier migration of this directory, for its function bodies (downgrade)."""
    spec = importlib.util.spec_from_file_location(
        f"_shifde_prev_{module}", Path(__file__).with_name(f"{module}.py")
    )
    if spec is None or spec.loader is None:  # pragma: no cover - file sits next to this one
        raise RuntimeError(f"cannot load {module}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _or_replace(ddl: str) -> str:
    return ddl.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def upgrade() -> None:
    _run(
        "CREATE POLICY insert_by_platform_admin ON shifde.tenants AS RESTRICTIVE FOR INSERT "
        "WITH CHECK (shifde.is_platform_admin_ctx())",
        MAY_BIND_GROUP,
        "REVOKE ALL ON FUNCTION shifde.shifde_may_bind_group(uuid, uuid) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION shifde.shifde_may_bind_group(uuid, uuid) TO shifde_app",
        CHECK_ROLE_BINDING,
        MY_MEMBERSHIPS,
        RESOLVE_SESSION,
        REVOKE_USER_SESSIONS,
    )


def downgrade() -> None:
    identity_0002 = _previous("identity_0002_definers")
    identity_0005 = _previous("identity_0005_platform_groups")
    tenancy_0001 = _previous("tenancy_0001_tenancy")
    _run(
        _or_replace(identity_0002.REVOKE_USER_SESSIONS),
        _or_replace(identity_0002.RESOLVE_SESSION),
        _or_replace(tenancy_0001.MY_MEMBERSHIPS),
        identity_0005._CHECK_ROLE_BINDING,
        "DROP FUNCTION shifde.shifde_may_bind_group(uuid, uuid)",
        "DROP POLICY insert_by_platform_admin ON shifde.tenants",
    )
