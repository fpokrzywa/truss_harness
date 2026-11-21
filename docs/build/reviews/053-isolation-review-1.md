APPROVED

Isolation review 1, job 053 (#56, PR #119), branch `factory/053-arch-m11-rest-data` at `10e8033`, diff `origin/main...HEAD`. Reviewer: isolation-reviewer (read-only; text saved by the lead).

No path found for one organization to reach another organization's data.

1. Organization context: the export route uses the same `ScopeDep` / `OntologyService` path as `/query`; `app.tenant_id` is set per transaction and re-applied by the `after_begin` hook in `core/db.py`, so the extra commits in `export` keep it. No migrations, grants or RLS changes.
2. Check order (`backend/src/shifde/ontology/service.py:514-551`): visibility (`arts.artifact`, 404 when not visible) first, then `data:export` and export policies. Another organization's id gets the unknown answer with nothing written.
3. Policy lookup: `export_row_cap` (`backend/src/shifde/governance/service.py:100-112`) uses `repo.list_policies` filtered by `sc.tenant_id`, on an RLS table.
4. Cursor: bound to the (already org-checked) version id plus the query body; a cursor from another organization fails validation.
5. Audit: recorded in `sc.tenant_id`; details hold no row values; `object_type` truncated to 200 characters.
6. Tests: route in the cross-client matrix (`backend/tests/ontology/test_routes_cross_client.py:234`); `backend/tests/ontology/test_export.py` checks B's owner exporting A's ontology matches a random id and writes nothing in A; scoping entry `backend/tests/authz/scoping/test_workspace_routes.py:412`.

Non-blocking policy notes: (a) `max_rows` caps one page, not the total (follow-up #135); (b) `data:export` governs the labelled, audited channel, not access to the rows, which `/query` already gives `ontology:query` holders (B-2647).
