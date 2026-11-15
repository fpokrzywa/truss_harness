# ontology isolation review 1 (part 1, `2e03702..380d1dc`)

Reviewer: `isolation-reviewer` (opus), 2026-11-15. Saved by the lead from the reviewer's report (the reviewer is read-only).

**Verdict: APPROVED.** Client A cannot reach client B's ontology rows (data, mappings, policies, principal attributes, lineage, materializations) or B's connections through the six tables, `ontology/repository.py`, the build mapping/info functions or `engine.py`. No must-fix.

## Checked (live catalog of `shifde_verify3` at `380d1dc`, as `shifde_owner`)

1. All six tables owned by `shifde_owner`, RLS enabled and forced, one permissive `tenant_isolation` policy each (USING and WITH CHECK `tenant_id = shifde.current_tenant_id()`), no all-rows policy; word for word SETH `p07_0001`.
2. `shifde_app` grants equal SETH's: mappings and policies SELECT/INSERT/DELETE + column UPDATE without `tenant_id`/`id`/identity columns; `ontology_version_info`, `ontology_query_lineage` SELECT/INSERT with append-only triggers; `ontology_principal_attributes` SELECT/INSERT + column UPDATE; `ontology_materializations` SELECT.
3. Every client-to-client reference is composite (`(tenant_id, ontology_version_id)`, `(tenant_id, connection_id)`, `(tenant_id, ontology_id, kind)`); every unique key reachable by `ON CONFLICT` starts with `tenant_id` (`uq_ontology_mappings__target`, `uq_ontology_materializations__type`, PKs of `ontology_version_info`, `ontology_principal_attributes`); remaining `id`-only PKs take server-made `uuid7` only.
4. `ontology_mappings_guard`: invoker, `search_path=pg_catalog, pg_temp`, EXECUTE only `shifde_owner`, explicit `v.tenant_id = COALESCE(NEW.tenant_id, OLD.tenant_id)`, so it holds for the BYPASSRLS owner.
5. `test_rls_schema.py` diff empty; all 7 `deliberate_changes.yaml` entries honest (3 keys + 3 indexes verified in the catalog; deferred `fk_ontology_lineage__run`).
6. Every read and write in `ontology/repository.py` and the build mapping/info functions filters on `tenant_id`; inserts override `tenant_id`; `upsert_mapping`/`insert_ontology_info` require the client's own version; conflict target starts with `tenant_id`.
7. `engine.py` identical to SETH apart from F-04; runs nothing itself; connection ids come from client-tied frozen mappings; all values bound. `exact_number` correct (`'9007199254740993'` exact int; `0.1`, `1.5` floats; `1e400` Decimal).
8. `tests/ontology` 58 passed; ontology rows of `tests/build/test_routes_cross_client.py` 30 passed with positive controls. Mutation (reviewer's): tenant filter removed from `get_policy`/`update_policy` fails `test_reads_find_only_...[b]`/`[owner]` and `test_a_cannot_change_bs_rows_through_the_repository[b]`/`[owner]`.

## Follow-ups

- **F1 (part 2)** `ontology/repository.py:82-114`: `update_policy` filters `tenant_id` and `id` but not `ontology_id`; add `AND ontology_id = :o` to both UPDATEs or keep and test SETH's `get_policy(tenant, ontology, policy)` pre-check (else a caller allowed on ontology X rewrites a policy of Y in the same client).
- **F2 (part 2)** SETH `ontology/service.py:312` `_usage` reads `runs` by id with no tenant predicate; port with `AND tenant_id = :t`.
- **F3 (part 2)** pass `self.sc.tenant_id` to every repository call; `_conn_row` through `ConnectionService.get_row` then `open_trusted`; `_typed_pk` uses `engine.exact_number`; `frozen_mappings` only with a version checked by `_version`; `subject_ids_covering`/`get_attributes` client-scoped; lineage routes check the row's `ontology_id` against a readable ontology (`get_lineage(tenant, id)` has no ontology filter); cross-client route tests for every policy, attribute, lineage, query and object route (same 404 for B's id and unknown; B unchanged).
- **F4 (runtime)** `ontology_0001_ontology.py:201-220`: until `fk_ontology_lineage__run` exists, `insert_lineage` accepts any `run_id` (stored only under A); part 2 passes only a `run_id` resolved through the client's own run; the runtime migration adds the key (checking or cleaning existing rows) and removes the yaml entry.
- **F5 (part 2/app, robustness)** `engine.py:135-136`: `int(text)` raises `ValueError` above 4300 digits (500); catch and fall back to `Decimal`.
- **F6 (part 2, intra-client)** migration line 207: `ontology_query_lineage.ontology_id` not tied to `ontology_version_id` (as SETH); optional composite key to `artifact_versions (tenant_id, id, artifact_id)`.
