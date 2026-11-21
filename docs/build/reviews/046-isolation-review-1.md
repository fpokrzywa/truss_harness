# Job 046 (issue #39) isolation review 1

Reviewer: isolation-reviewer (read-only; the lead recorded this from its report). Diff: `origin/main...d72d4dc9b73d6f73af0149e0d5c20b96b2743cd9`. Verdict: **APPROVED**. Code read, no tests run.

## Checked
- `tenants` keeps forced RLS; `tenant_isolation` (reads and writes) admits only `id = current_tenant_id()` or the platform-admin flag; unchanged by the diff. Tests: A-context write to B and no-context write both affect 0 rows.
- `shifde_app` can update only `name, description, status, row_version, settings`; any settings write names the column so the column trigger fires. Only other trigger `set_updated_at` never touches settings. Upsert fires the update trigger and also hits restrictive `insert_by_platform_admin` (tenancy_0002:215). Plain insert needs the platform-admin flag already. Non-object/NULL refused.
- `-` and `jsonb_typeof` resolve via `search_path = pg_catalog, pg_temp` (functions/operators never resolve from pg_temp); `shifde.is_platform_admin_ctx()` schema-qualified. App role owns nothing and has no CREATE (dbrole.py startup check).
- Superuser/BYPASSRLS exemption (migration 35-38) unreachable from the app role: `CURRENT_USER` check, dbrole.py refuses membership in privileged roles; `session_replication_role` needs superuser; no SECURITY DEFINER function writes `shifde.tenants`.
- Platform-admin flag (32-34) is the same `app.platform_admin` setting every platform-admin policy already relies on; set only through `set_platform_admin` / `platform_admin_context` after the PDP check.

## Non-blocking
1. migration 35-38: a future SECURITY DEFINER function owned by `shifde_owner` writing `tenants.settings` would skip the guard. Require a key check in it at its isolation review, or base the exemption on `row_security_active('shifde.tenants')`.
2. migration 32-34: guard is as strong as the app code that sets the flag; same trust model as the rest of the schema (B-2717).
3. Tests: no test that an owner update still passes, none for a refused `ON CONFLICT DO UPDATE` as `shifde_app`. Cheap, not required.
