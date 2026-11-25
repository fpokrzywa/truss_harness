# Isolation review 1: job 177 (#359, PR #372, head e90db68)

Saved by the lead from the read-only isolation-reviewer, unchanged.

APPROVED

Scope: `git diff origin/main...HEAD` at e90db68 (origin/main 8212720). It changes 2 source files, adds 2 test files, edits 1 test file, adds 1 DECISIONS row. No migration, grant, policy, route, worker or frontend change. I did not read build.md and changed no files; the worktree is clean.

No blocking findings.

What I checked
- **Another client.** `_visible_client` (tenancy/dependencies.py:40-55) still picks the client from the key or membership. `_load` reads only bindings stored in `_tenant_id`, under RLS. `workspace_ids_of_projects` filters on `tenant_id`. `ck_role_bindings__tenant_scope` forces `scope_id = tenant_id`, so `scope_type == "tenant"` at pdp.py:389 cannot name another client.
- **Another workspace or project.**
  - The only consumers of `ancestor_workspace_ids` are tenancy/service.py:217, 465, 475, 485, 616 and 751. `authorize`, `effective_permissions`, `missing_permissions` and `visible_scopes` are unchanged and still stop at the mask.
  - I read the callers of `scope_ref_from_slugs` that can now return a workspace-only or project ref for such a key: iam, appgen, assist, board, connections, delivery, evaluation, models_admin, packaging, runtime and runtime/action. Each authorizes the ref or row, or filters by the masked `visible_scopes`.
  - Workspace-level objects (shared connections, targets) are still refused: `placement` authorizes the row, and the list SQL keeps the `vws`/`vps` filter.
- **Beyond the person.** The branch fires only when the owner holds `workspace:read` at organization level after the key-scope filter (`_load`), so each added workspace is one the person reads already. Expired bindings, suspended members and unbound groups drop out in `load_grants`.
- **Masked-ancestor metadata.**
  - `WorkspaceOut` and `OrgOut` expose only id, slug, name, description, status, row_version and dates, never settings. `effective_settings` has no caller that passes a workspace.
  - Dashboard counts stay masked (own workspaces and projects; members null).
  - Dashboard activity uses `granted_workspace_ids` unless a readable project is named, and then rows are restricted by `project_id`.
- **Write and use.** Workspace PATCH and project create go through `pdp.require` on the workspace ref and are denied with `resource_scope`. The answer is 403 instead of 404, because the ancestor is visible.
- **Non-key principals.** With `mask is None`, `ancestor_workspace_ids` equals `granted_workspace_ids`, which equals the old body, and the dashboard expression reduces to the old `ws_only`. A masked principal without organization-level `workspace:read` is also unchanged.
- **MCP.** `server.py:138` still drops the mask only for the `mcp:access` check. Tools use the masked PDP.
- **Edited existing test.** Only tests/mcp_access/test_sp7_tools.py:287-340. The "scoped key" and "p1 member" cases keep every assertion, one parameter is added, one comment is reworded, and the branch is split without changing behaviour. No test is removed, skipped or loosened.

Runs
- `tests/authz tests/tenancy tests/mcp_access` at head, `-n 8`: 258 passed.
- Mutations came from a temporary pytest plugin outside the repo, since removed:
  - Change reverted at PDP level (ancestor = granted), new and edited tests: 16 failed, 11 passed. The failures include the `organization grants only` case, the HTTP read test, the MCP far-workspace test and the MCP other-client test. The passes are mostly the negative and unchanged-behaviour cases, as expected.
  - Any organization-level grant accepted instead of `workspace:read`: 5 failed.
  - Dashboard restriction defeated (`granted_workspace_ids` widened to the mask): `test_the_dashboard_of_the_key_shows_no_activity_beyond_its_project` failed, with 10 unit tests.

Notes (non-blocking)
1. backend/src/shifde/authz/pdp.py:371 - `_mask_workspace_ids` resolves mask projects against this client, but returns `mask.workspace_ids` as they are. Before, the result was only intersected with grants; now it is unioned, so a stale or foreign workspace id would appear in `ancestor_workspace_ids()`. No leak today: key creation validates the ids (admin/credentials.py:218-224), every consumer looks rows up by `tenant_id` under RLS, and `_require_some_grant` could only turn true for a client where the owner already holds `workspace:read`. Fix: filter `mask.workspace_ids` by `tenant_id` too, as for projects.
2. backend/tests/authz/test_masked_ancestors.py:148-164 - the cross-client unit test covers another client's project id in the mask, not another client's workspace id. Add that with note 1.
3. backend/src/shifde/tenancy/service.py:779-785 - the safety of `activity_ws_only` depends on `project is not None` implying a row filter on `project_id` (audit/repository.py:46). That holds and is tested. If `recent_activity` ever loses that filter, the key would see the whole workspace.
4. Existing behaviour, recorded in B-4040, not widened here: a project-grant holder, or a masked key whose owner also holds a project or workspace grant, sees the whole ancestor workspace's dashboard activity. This shows sibling projects' labels within the same client. It deserves its own issue.
5. Existing behaviour, recorded in B-4040: a write to an invisible workspace answers detail `resource not found`, an unknown slug answers `workspace not found` (service.py:451 vs pdp.py `require`). This is a same-client existence oracle for workspace slugs. `test_the_key_writes_neither_its_workspace_nor_another` asserts only status 404 for SECOND, so it does not catch it. Same follow-up.
6. `GET /orgs/{org}` is now 200 for such a key (it was 404). This is its own client's id, name and description, which its owner reads, and matches what a project member gets. It is slightly more than the issue's literal "P1's workspace by slug and P1"; I accept it as recorded in B-4040.
7. backend/tests/mcp_access/test_project_key.py covers a person's key over HTTP and MCP. Service-account and execution principals are covered only at PDP level (test_masked_ancestors.py:125-135). Sufficient, since both use the same PDP path.
