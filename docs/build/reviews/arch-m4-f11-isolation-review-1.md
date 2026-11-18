# ARCH M4 / F-11 isolation review 1: APPROVED

**Branch:** factory/011-arch-m4-f-11 (5407509) vs origin/main
**Scope:** `git diff origin/main...HEAD`. Commits 854eb33, c6aa69b, 5407509.

## What was checked
- **Role / startup check:** `backend/src/shifde/cli/main.py` `maintenance_purge` opens `Database(settings.database_url, pool_size=1, max_overflow=0)`.
  - The default `verify_role=True` is kept, so `check_app_role` runs. It refuses superuser, BYPASSRLS, owner, CREATE-capable and data-role connections, and any role those can reach.
  - The checkout hook still runs `DISCARD ALL` and the preset-context refusal.
  - It never touches `effective_migration_url` or `_owner_session`.
  - `UnsafeDatabaseRole` exits 1 ("refusing to purge"). `test_a_privileged_role_is_refused` checks this and confirms the data is left as it was.
- **Platform-administrator context:** `backend/src/shifde/cli/maintenance.py` `purge`.
  - Used for one statement only, `SELECT id, slug FROM shifde.tenants`.
  - `platform_admin_context` clears the flag in the session context and in the database before `session.commit()`. Every later transaction's `after_begin` applies a context with `platform_admin=False`.
  - Only ids and slugs leave that transaction.
- **Per-client pass:**
  - Each client gets `set_tenant(session, tenant_id)` in a fresh transaction, committed (or rolled back for a dry run) on its own; the finally block calls `forget_tenant`; on error the transaction is rolled back and re-raised, so no client context carries over.
  - Every statement also has an explicit `tenant_id = :t` predicate (`_ELIGIBLE` on `r` and the `root` join, `retention_rows`, `unscrubbed_counts`, `scrub_run_content`, `audit.record(tenant_id=...)`), so isolation does not depend on RLS alone.
  - `retention_policies` has ENABLE + FORCE RLS with `tenant_isolation` (USING and WITH CHECK on `current_tenant_id()`), so the legal-hold read under the client's own context cannot miss a hold.
- **Writes:** only the two `runtime_0002` scrubs (artifact content → `{"scrubbed": true}`, tool-invocation arguments → NULL); `run_artifacts_guard` still applies; nothing deleted; no new grants, policies, migrations, SECURITY DEFINER functions or owned objects.
- **New repository function:** `unscrubbed_counts` (`backend/src/shifde/runtime/repository.py`) is tenant-filtered and added to the cross-client CASES harness in `backend/tests/runtime/test_repository.py` (app role and owner).
- **Audit:** one `maintenance.retention.purged` event per client in its own chain, actor `shifde-admin:<OS user>`; details hold only that client's counts, cutoff and hold flag; `audit verify` on both chains is tested.
- **Routes, worker, caches:** none added or changed.

## Non-blocking notes
1. **Platform-admin flag without a principal** (`cli/maintenance.py`, `purge`, `with platform_admin_context(session)`): set without a PDP check. Not an escalation: holders of the `shifde_app` credential can already set `app.platform_admin`, and it only lists tenant ids and slugs. Suggest a comment that the CLI's trust boundary is the app DB credential.
2. **Legal-hold test only** (`tests/cli/test_maintenance_purge.py`, test (f)): the cross-client test protects A only via A's legal hold. Suggested extra test: A without a hold but a longer `retain_days` (or only recent runs), B purged, A byte-for-byte unchanged. The repository-level CASES test already covers the per-statement tenant filter.
3. **Test side effects** (`tests/cli/test_maintenance_purge.py`): each purge sweeps every tenant in the test database, so under `pytest -n` it can scrub other concurrently running tests' old-ended-run fixtures and add audit events to their chains. Test reliability, not exposure. Suggest an internal tenant filter for the CLI under test, or a separate database / serial marker.

## Verdict
APPROVED. No path lets one client reach, alter or purge another client's data. The command runs as `shifde_app` behind the startup role check, and nothing bypasses row-level security.

(Reviewer: isolation-reviewer. Saved by the lead; the reviewer is read-only.)
