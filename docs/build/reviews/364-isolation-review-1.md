# Isolation review 1, #364 (PR #386), 0bddc55

APPROVED

Isolation review 1 of #364 (PR #386), `lead/364-delete-guard-tenant` at 0bddc5584d312d39ec7009ac1dc78b853728e116 against f2af18b. One commit, six files, no source or route change. No blocking finding.

Paths are relative to S:/Development_2026/shi_work/agt_trus_harness/backend.

**(1) Cross-client delete as shifde_app**

1. The check is correct.
   - Location: `migrations/versions/runtime_0010_retention_guard_session_client.py` and `assist_0003_delete_guard_session_client.py`, in `GUARD`.
   - It is `shifde.current_tenant_id() IS DISTINCT FROM OLD.tenant_id`, schema-qualified. `current_tenant_id()` is STABLE, not IMMUTABLE, and its body is fully `pg_catalog`-qualified, so the setting is read again for each row.
   - It runs before any read of a policy. Nothing the caller controls runs between the check and the end of the guard: only `pg_catalog` functions and the RLS policy expression.

2. Attacks I ran as `shifde_app` on all eleven tables, switching to another client and to no client. No row went in any case.
   - Refused by the new check (42501, its own message):
     - DELETE inside a data-modifying CTE
     - `USING` with a volatile subquery
     - a `pg_temp` plpgsql function in WHERE
     - switch, savepoint, rollback, then switch again inside the DELETE
     - a nested set_config that yields NULL
   - Refused by RLS (42501): MERGE with the switch in ON or in WHEN.
   - 0 rows: cursor `FOR UPDATE`, switch, `DELETE ... WHERE CURRENT OF` on nine tables. The other two lack UPDATE, so the cursor was denied.
   - 22P02: a setting that is not a uuid.
   - My LATERAL variant was invalid SQL (42P10) on ten tables and not applicable to `ontology_query_lineage`. It proves nothing and is not counted.

3. Cascades, from the catalog.
   - The only cascade into a guarded table is `assist_proposals(tenant_id, request_id)` to `assist_requests`. It is a composite key, so it cannot reach another client. It is not deferrable.
   - The assist keys to projects and workspaces are RESTRICT. Every key to `runs` is `(tenant_id, ...)` NO ACTION, not deferrable.
   - A switch in RETURNING before the cascade is refused by the proposal's guard (tested).

4. Other paths, from the catalog.
   - The guard is the only BEFORE DELETE trigger on each table.
   - `shifde_app` has no TRIGGER, TRUNCATE or REFERENCES privilege in the schema, and no CREATE on `shifde` or `public`. There are no rules and no inheritance.
   - All eleven tables have RLS enabled and forced, are owned by `shifde_owner`, and have the single `tenant_isolation` policy with USING and WITH CHECK.
   - The only SECURITY DEFINER function containing `DELETE FROM` is `shifde_sync_idp_groups`, which per its name and role handles the idp group rows; I did not read its body. No function in `shifde` writes `app.tenant_id`.

5. Residual, accepted and recorded in B-4090: raw SQL as `shifde_app` can set the row's own client for the whole delete. The guard then applies that client's hold, floor and retention. The diff does not widen this.

**(2) Existing checks and downgrade**

6. Both bodies equal `runtime_0009`'s and `assist_0002`'s `GUARD` plus only the five-line check and its comment, placed after the cutoff parse and before the floor check. No check is removed, reordered or weakened. A cutoff that is missing or unparsable is still refused first, with the old text. The new refusal text names no client and reveals no hold or retention.

7. In the migrated database both guards are owned by `shifde_owner`, not SECURITY DEFINER, `search_path=pg_catalog, pg_temp`, with EXECUTE for the owner only. The eleven triggers are enabled and are BEFORE DELETE row triggers. No grant, policy or table changes.

8. Downgrade executes `PREVIOUS_GUARD`, pinned equal to the previous `GUARD`. The scratch-copy tests check body, owner, settings and triggers at each step, down and up. Neither revision has `depends_on`, and no migration on another local or remote branch collides with them.

**(3) Tests**

9. Mutation runs of `tests/schema/test_retention_delete_guards.py` (93 tests):
   - both checks removed at upgrade: 65 failed
   - assist check only removed: 13 failed
   - runtime check only removed: 52 failed
   - `<>` instead of `IS DISTINCT FROM`: 65 failed, so the no-client case is covered

10. Assertions removed by the diff are only the head pins that had to move.
   - `tests/cli/test_maintenance_delete.py:802,1033`: the `upgraded` pin moves to 0010 / 0003, and the old bodies are still asserted one downgrade later.
   - `tests/identity/test_schema.py`: the two head assertions move, and `runtime_0009` and `assist_0002` become `not in heads`.
   - Nothing is weakened. The new table list is pinned to the catalog together with `tenant_id` NOT NULL.

**(4) Legitimate callers**

11. `cli/maintenance.py:_each_client` sets the client before each pass, and `assist/repository.py:delete_requests` runs in that pass. These are the only `src` deletes of the guarded tables. Test clean-up and `lose_rows` use `session_replication_role = replica`. Owner deletes found in the tests set the client. The whole-family retention delete and the request cascade pass as `shifde_app`.

**Notes (not blocking)**

12. `tests/schema/test_retention_delete_guards.py:195-221` accepts either 0 rows or 42501, because the plan decides. This is acceptable: the remaining row is asserted each time and the strict forms are covered by the other tests.

13. The issue's command names `tests/runtime/test_schema.py`, which does not exist. Pytest then exits 5 having run nothing. The issue allows "the modules' own schema tests"; do not record that literal command as passed.

14. A schema-owner delete now also needs the client's setting (B-4089). It fails closed and no operational path relies on it.

**What I ran** (scratch worktree `.worktrees/364-iso1`, now removed)

- `pytest -n 8` on `tests/schema/test_retention_delete_guards.py`, `tests/cli/test_maintenance_delete.py`, `tests/identity/test_schema.py`, `tests/assist/test_retention.py` and `tests/assist/test_routes_cross_client.py`: 193 passed.
- `pytest -n 8` on `tests/schema`, `tests/runtime/test_capture_off.py`, `tests/runtime/test_lifecycle.py`, `tests/cli/test_catalog_rules.py`, `tests/cli/test_backup.py` and `tests/cli/test_restore_row_security.py`: 460 passed, 1 skipped.
- Catalog probe: 1 passed. Attack probe: 11 passed. Both probe files deleted.
- Four mutation runs, as in item 9, reverted.
- `ruff check` on migrations, tests/schema, tests/cli and tests/identity: passed. `ruff format --check .`: passed.
- Not run: full suite, `factory/checks.sh`.
- Left behind: the dev database `shifde_364_iso1`, created by `dev_db.py env`. I did not drop it.
