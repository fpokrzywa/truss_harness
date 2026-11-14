"""Tenancy, part 2b: isolation review 2 follow-ups (who may name an owner, fail closed).

Revision ID: tenancy_0003
Revises: tenancy_0002

* Review 2 item 1 (B-1015): the group-binding check fails closed. `shifde_may_bind_group`
  answers `false`, never NULL (no client context, no principal), and `check_role_binding()`
  refuses unless the answer `IS TRUE`.
* Review 2 item 2 (B-1016): binding a person to the `tenant_administrator` system template at
  organization scope names an owner (B-1010), so `check_role_binding()` lets only the
  platform-administrator context or that client's owner do it; `role:manage` (a group-held
  tenant administrator included) is not enough. New definer `shifde_is_client_owner(uuid)`:
  the owner predicate `shifde_may_bind_group` used inline, in the current client only, false
  otherwise.
* Review 2 item 4 (B-1018): `shifde_active_group_ids(uuid)` returns nothing for a person whose
  membership in the current client is not active, so the PDP grants nothing through a group
  after a membership is suspended (the visibility rule of `shifde_my_memberships`, B-1009).

Rollback: drops the new definer and restores `shifde_may_bind_group` and `check_role_binding`
(tenancy_0002) and `shifde_active_group_ids` (identity_0005).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "tenancy_0003"
down_revision = "tenancy_0002"
branch_labels = None
depends_on = None

_LOCKED = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
_LIVE = "(rb.expires_at IS NULL OR rb.expires_at > pg_catalog.now())"

# The owner of a client (B-1010): an active person with an active membership there and a live
# direct binding of the `tenant_administrator` system template at organization scope; only for
# the current client and the current principal. False, never NULL, in every other case.
IS_CLIENT_OWNER = f"""CREATE FUNCTION shifde.shifde_is_client_owner(p_tenant uuid)
   RETURNS boolean
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT COALESCE(
        p_tenant IS NOT NULL AND p_tenant = shifde.current_tenant_id()
         AND shifde.current_principal_id() IS NOT NULL
         AND EXISTS (SELECT 1 FROM shifde.memberships m JOIN shifde.users u ON u.id = m.user_id
                      WHERE m.tenant_id = p_tenant AND m.user_id = shifde.current_principal_id()
                        AND m.status = 'active' AND u.status = 'active')
         AND EXISTS (SELECT 1 FROM shifde.role_bindings rb JOIN shifde.roles r ON r.id = rb.role_id
                      WHERE rb.tenant_id = p_tenant AND rb.subject_type = 'user'
                        AND rb.subject_id = shifde.current_principal_id()
                        AND rb.scope_type = 'tenant' AND rb.scope_id = p_tenant
                        AND r.tenant_id IS NULL AND r.key = 'tenant_administrator'
                        AND {_LIVE}),
        false) $$"""

MAY_BIND_GROUP = f"""CREATE OR REPLACE FUNCTION shifde.shifde_may_bind_group(
       p_tenant uuid, p_group uuid)
   RETURNS boolean
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT COALESCE(
        shifde.shifde_is_client_owner(p_tenant)
         AND EXISTS (SELECT 1 FROM shifde.groups g WHERE g.id = p_group AND g.status = 'active'),
        false) $$"""

ACTIVE_GROUP_IDS = f"""CREATE OR REPLACE FUNCTION shifde.shifde_active_group_ids(p_user uuid)
   RETURNS SETOF uuid
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT g.id FROM shifde.group_members gm JOIN shifde.groups g ON g.id = gm.group_id
       WHERE gm.user_id = p_user AND g.status = 'active'
         AND shifde.current_tenant_id() IS NOT NULL
         AND EXISTS (SELECT 1 FROM shifde.role_bindings rb
                      WHERE rb.tenant_id = shifde.current_tenant_id()
                        AND rb.subject_type = 'group' AND rb.subject_id = g.id)
         AND NOT EXISTS (SELECT 1 FROM shifde.memberships m
                          WHERE m.tenant_id = shifde.current_tenant_id() AND m.user_id = p_user
                            AND m.status <> 'active') $$"""

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
          IF NEW.scope_type = 'tenant'
             AND EXISTS (SELECT 1 FROM shifde.roles r WHERE r.id = NEW.role_id
                         AND r.tenant_id IS NULL AND r.key = 'tenant_administrator')
             AND NOT shifde.is_platform_admin_ctx()
             AND shifde.shifde_is_client_owner(NEW.tenant_id) IS NOT TRUE THEN
            RAISE EXCEPTION 'naming an owner needs the owner or the platform administrator'
              USING ERRCODE = 'insufficient_privilege';
          END IF;
        ELSIF NEW.subject_type = 'group' THEN
          IF shifde.is_platform_admin_ctx() THEN
            IF NOT EXISTS (SELECT 1 FROM shifde.groups g WHERE g.id = NEW.subject_id) THEN
              RAISE EXCEPTION 'subject group is not a platform group'
                USING ERRCODE = 'check_violation';
            END IF;
          ELSIF shifde.shifde_may_bind_group(NEW.tenant_id, NEW.subject_id) IS NOT TRUE THEN
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
        IS_CLIENT_OWNER,
        "REVOKE ALL ON FUNCTION shifde.shifde_is_client_owner(uuid) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION shifde.shifde_is_client_owner(uuid) TO shifde_app",
        MAY_BIND_GROUP,
        ACTIVE_GROUP_IDS,
        CHECK_ROLE_BINDING,
    )


def downgrade() -> None:
    tenancy_0002 = _previous("tenancy_0002_service")
    identity_0005 = _previous("identity_0005_platform_groups")
    _run(
        tenancy_0002.CHECK_ROLE_BINDING,
        _or_replace(identity_0005.FUNCTIONS["shifde_active_group_ids(uuid)"]),
        _or_replace(tenancy_0002.MAY_BIND_GROUP),
        "DROP FUNCTION shifde.shifde_is_client_owner(uuid)",
    )
