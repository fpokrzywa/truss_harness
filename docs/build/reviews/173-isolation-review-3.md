# Isolation review 3, job 173 (#286 SP8-1) at f83be49

**APPROVED** (no blocking finding). The whole diff 911533d..f83be49 was read. The branch diff against origin/main (5cd5e1a) was read where it touches rendering, packaging, planning, targets and claims, executor dispatch and tests. All paths below are under S:/Development_2026/shi_work/agt_trus_harness/.worktrees/173-sp8-1-kinds-claims/.

**One merge-order hazard must be acted on when job 180 meets this branch (finding 1).**

## Checks asked
1. Nothing loosened or removed.
   - Round 3 removes five test lines: one docstring line and four import lines, each re-added wider. No assertion, expected value or parametrization changed. `test_the_generator_version_is_appgens` passes unchanged.
   - Earlier rounds' edits were re-read: `identity/test_schema.py` heads, `test_solutions.py`, `test_target_kind_db.py` (exact CHECK text, now five values), `test_main.py`. Each assertion is kept or extended to the spec's new kinds.
   - `tests/schema`, the row-level security tests and the startup role check are untouched. Cross-client tests are only added to.
   - Merge 911533d: the remerge diff touches docs/build/DECISIONS.md only. From 69e4312 to 911533d, the only source change under delivery, executor, adapters/native, packaging, core or migrations is main's line in `adapters/native/databricks/source.py`.
2. A `docker.compose/v1` release or target on this head:
   - Render: `backend/src/shifde/appgen/render.py:153` refuses by kind, before anything else in the manifest is read. `render` has two callers, `appgen/service.py:537` (preview) and `delivery/plans.py:415`. Preview compares pin visibility and reads `scoped_release` first, so client B gets an unknown id's 404 (tested for all six seals).
   - Plan: `delivery/plans.py` `request` refuses any non-Databricks target. `check_target_kind` (:131, called at :450) refuses any other manifest kind, before both the template path and the render path. `insert_artifact` and `insert_plan` have this one caller, and `TARGET_KIND` is hard-coded in the artifact row.
   - Jobs: the `plan` job comes only from `plans.py:373`. Apply, observe, verify and reconcile jobs all need an existing plan, so none can be queued for such a target.
   - Lease: if a job existed anyway, `Runner._docker` (also `_fabric`, `_palantir`) checks the grant's tenant, target and entry, then raises `execution_failed` before any adapter, platform or checkpoint call. `DISPATCH` is the only path to an adapter.
   - Packaging: a Docker release can still be sealed (spec requirement 8, B-4005). That is a manifest row under the client's row-level security; no deployable archive is built.
   - `generator_version_for("docker.compose/v1")` now returns a version. It has no caller in `src`.
3. Claims, addresses and targets: round 3 changes nothing here. Claim keys are per platform kind, through the unchanged `delivery_claim_platform` definer, and the fixed 409 names nobody. The Palantir suffix list is used for `palantir` only. Targets are read and written with `tenant_id` plus row-level security. `resolved_for_release` still reads only under the caller's scope. Only protected-path files: migrations `delivery_0008` and `packaging_0005`, both already reviewed and unchanged since review 2.

## Findings
1. NOT BLOCKING here; BLOCKING for whichever of 173 and 180 (#180, SP6-6) merges second. `backend/src/shifde/packaging/schemas.py:48-52` sets `TARGET_ADAPTERS["docker.compose/v1"] = "databricks"`, meaning the data adapter. On `factory/180-sp6-6-delivery-plans`, `delivery/plans.py` `check_target_kind(manifest, platform_kind)` reuses `TARGET_ADAPTERS` as the deployment platform and drops the `!= "databricks"` target check.
   - Scenario: after both merge, a Docker release passes the kind gate on a Databricks target. With a `/2` generator version, only render.py:153 stops it. With no generator version (`no-app`, `no-version`), `app_generator_version` is None, so the SDK template is stored as a `docker.compose/v1` artifact and planned and leased on the client's Databricks workspace.
   - Scope: API-sealed Docker releases always carry `app`, so this needs a manifest not sealed by today's API. It is a wrong-platform deploy within one client, not a cross-client read.
   - Probe: I put 180's gate into the scratch worktree. `test_a_docker_release_is_planned_on_no_target[databricks-*]` failed 6 of 6: four answer `generator_version_unsupported`, and `no-app` and `no-version` are accepted (plan `requested`). So this branch's tests catch it.
   - Git reports no conflict in `delivery/plans.py` for this pair (it does conflict in render.py, runner.py, executor/support.py, test_routes_cross_client.py, DECISIONS.md), so it merges silently.
   - Fix at the second merge: key the deploy gate on its own map (`databricks.app/v1` -> `databricks`, `snowflake.native-app/v1` -> `snowflake`, every other kind refused), not `TARGET_ADAPTERS`. Keep all 12 cases of that test refused with no write (180 renames the code to `target_kind_mismatch`, so an expected-value change needs a ruling). Give the `monkeypatch` lambda in `tests/delivery/test_generated_app.py` (`test_a_docker_release_past_the_kind_gate_is_not_rendered`) 180's second argument. Keep the Docker refusal first in `render` when resolving render.py.
2. NOT BLOCKING, for SP8-9 (#294): without the line at render.py:153, a Docker manifest with its own version falls through to the Databricks bundle (the last branch of `render` is the default, not a named kind). #294 should dispatch by kind with a refusing default.
3. Carried over, unchanged, not reachable today: review 2 notes 3 (`DockerBindings.workspace_host` is not claim-checked, #294) and 4 (nested Palantir suffixes, operator configuration only, #292).

## Commands (scratch detached worktree .worktrees/173-iso3 at f83be49, since removed)
- `git worktree add --detach`, `python scripts/dev_db.py env`, `uv sync --frozen`: exit 0, 0, 0.
- `uv run --frozen pytest -q -n 8 tests/packaging tests/appgen tests/delivery tests/executor tests/adapters/native tests/identity/test_schema.py tests/schema tests/authz/test_catalog.py tests/app/test_routes.py`: 2054 passed, 7 skipped (opt-in live or Docker, POSIX-only file modes), exit 0.
- Mutation 1 (render refusal off), `-k docker` over the four changed test files: 20 failed, 16 passed, so the new tests guard the refusal.
- Mutation 2 (180's kind gate), `tests/delivery/test_generated_app.py -k docker`: 6 failed, 7 passed (finding 1).
- `git merge-tree --write-tree f83be49 origin/main`: DECISIONS.md conflict only (main's newer commits are docs only). Against 180: the conflicts listed in finding 1.
- `git worktree remove --force .worktrees/173-iso3`: exit 0. The job worktree is clean at f83be49.
- Full suite and front end: not run.
