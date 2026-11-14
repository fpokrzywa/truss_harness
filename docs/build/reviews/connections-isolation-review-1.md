# Connections part 1 (data layer): isolation review 1

Diff `2c8b763..60074c3` (branch `connections`). Reviewer: isolation-reviewer (opus), 2026-11-14. Saved by the lead from the reviewer's report.

**Verdict: APPROVED** (0 blocking, 3 follow-up). Clean detached worktree at `60074c3`: `tests/connections tests/schema tests/secrets tests/vendors tests/installation -n 6` 263 passed; `tests/core/test_skeleton.py` (downgrade to base and back) passed. Probes as `shifde_app` and `shifde_owner` on per-worker scratch databases on 55450.

## What holds

- **Ownership and RLS:** four tables owned by `shifde_owner`, RLS enabled and forced; `connections`, `connection_tests`, `connection_discoveries` each have `tenant_isolation` (`tenant_id = current_tenant_id()` in USING and WITH CHECK).
- **`egress_policies`:** SELECT `tenant_id IS NULL OR own` (only the installation default is extra); FOR ALL write: own, or the NULL row only under the platform-administrator context. From A: UPDATE of the NULL row touches 0 rows; INSERT with NULL or B's tenant gives 42501.
- **Grants (probed):** `shifde_app` has SELECT and INSERT; column-level UPDATE only on `connections` and `egress_policies`, never `tenant_id`, `workspace_id`, `project_id`; no DELETE, TRUNCATE, REFERENCES, TRIGGER. `connections_guard` is SECURITY INVOKER, owner-only EXECUTE.
- **Composite keys:** secret, egress-policy, workspace, project keys and the test/discovery keys to `connections` are composite; `connections.tenant_id` is NOT NULL, so an installation secret or the installation default can never match (`test_the_composite_keys_hold_without_the_guard_or_rls`).
- **Guard probes:** under A's context plus the platform-admin context, binding the installation secret, the installation default, B's secret or B's vendor, and repointing through `INSERT ... ON CONFLICT DO UPDATE SET vendor_id = <B's vendor>`, all give 23514 with the unknown-id message; the guard names the tenant explicitly and refuses B's vendor even as the owner.
- **No existence signal:** naming A's connection from B in `connection_tests` gives the unknown-id 23503; name keys unique per client; repository reads/updates keyed by `tenant_id` as well as RLS.
- **`PLAIN_FOREIGN_KEYS` entry `connections_vendor_id_fkey` is sound** and does not weaken the schema test: the guard fires on every INSERT and every UPDATE naming `vendor_id` (incl. upsert); `shifde_app` cannot DELETE vendors or update their `id`/`tenant_id`; a composite key is impossible (global vendors have a NULL tenant).
- **Downgrade:** drops the four tables and the guard; downgrading `vendors` removes `connections` first; upgrading again recreates exactly one installation default.
- **Tests start from own rows** for reads and bindings.

## Follow-ups (all owner: connections part 2)

1. `connections/egress.py:115-117` (`build_guard`): with no or the wrong client context, `get_default_policy(session, tenant_id)` returns None under RLS and the client's own restrictive default is silently left out. Fix: require `shifde.current_tenant_id() = tenant_id` (raise otherwise), with a test.
2. `tests/connections/test_cross_client.py:207-262`, `:126-159`: "B's row unchanged" checks for `update_connection`, `record_test_outcome`, `update_egress_policy` lack an own-row positive check in the same test. Fix: update A's own rows first and assert the change.
3. `connections_0001_connections.py:82-100`: nothing ties a secret's scope to the connection's workspace/project; SETH relies on `ConnectionService._check_secret` (`secret:use` on the secret's scope, `hide_unless="secret:read"`). Fix: port `_check_secret` for create and patch, with a cross-workspace bind refusal test.
