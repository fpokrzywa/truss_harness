# Isolation review 4, job 173 (#286, PR #354), bbbe14d

APPROVED

# Isolation review 4, job 173 (#286 SP8-1) at bbbe14d (merge 39036e7 of f83be49 + f2af18b, then the gate commit)

No blocking finding. Review 3 finding 1 (the merge-order hazard) is closed. Paths below are under S:/Development_2026/shi_work/agt_trus_harness/.worktrees/173-sp8-1-kinds-claims/.

## 1. Merge 39036e7: nothing lost
- Merge base is 8212720: 37 files changed on the branch, 59 on main, 11 on both. No file outside those sets changed. Every file changed on one side only is blob-identical to that side.
- Seven files conflicted: CLAUDE.md, `appgen/render.py`, `executor/runner.py`, `tests/delivery/test_routes_cross_client.py`, `tests/executor/support.py`, `tests/identity/test_schema.py`, DECISIONS.md. Four merged on their own: openapi.json, `native/contracts.py`, `delivery/service.py`, `tests/delivery/test_targets.py`. I compared all 11 line by line, and the whole remerge diff was read.
  - The only lines of either side not found verbatim are rewrapped docstrings and the one CLAUDE.md executor line. All content of both sides is present: five credential entries, staged deep health with the Cortex model, and the fail-closed kinds.
  - `ErrorCode` keeps main's `snowflake_patch_limit`; `TargetKind` keeps the branch's five kinds.
- AST comparison of top-level functions and classes in the changed Python files: none missing or duplicated. The only merged test function differing from a parent is `test_a_head_named_in_depends_on_is_not_a_version_head`, with 48 asserts against 47 and 46; it is the union.
- `tests/schema`, `factory/`, `.claude/` and `core/dbrole.py` are identical to f2af18b. The only migration change from f83be49 is main's assist_0002. Cross-client tests of both sides are kept (SP8-1 and SP6-6).
- The job 180 gate tests in `test_release_gate.py` are unchanged.

## 2. Gate commit
- `backend/src/shifde/delivery/plans.py:104` `PLATFORM_OF_KIND` holds exactly the two ruled pairs. :165 is the only comparison, and a non-string or missing kind is refused. `TARGET_ADAPTERS` is gone from delivery; it is used only in `packaging/service.py:846,862` (data adapter).
- Order in `request` (:356): `plannable` (own tenant, `delivery:plan:request`, hidden as `target not found`), active, unresolved, then `_gate`. `_gate` runs `scoped_release` (caller's read), withdrawn, workspace and project, evaluation, then `check_target_kind` at :528. All of it precedes render, artifact, plan, job and audit. The only earlier commit is the existing settle sweep, unchanged.
- No other path:
  - `insert_plan` and delivery's `insert_artifact` each have one caller (`plans.py:375,387`). The assist accept (`assist/service.py:1270`) goes through the same `request`. Deployment jobs need an approved plan.
  - Behind the gate, `render` still refuses Docker first, the runner's `DISPATCH` fails closed for fabric, palantir and docker, and `REQUIRED_BY_PLATFORM` (`delivery/service.py:896,1188`) lists only Databricks and Snowflake, so another kind can never become `verified`.
  - Another client's target or release stays the unknown id's 404 (existing tests pass).
- Test edits are exactly as ruled:
  - `test_generated_app.py:234` (12 cases) and `test_targets.py:730` (3 cases) change only the expected code. `_refused` still requires status 409 and unchanged write counts; `test_targets.py` still requires rows (0, 0, 0).
  - The lambda takes the second argument, plus one docstring in `sp8.py`. No other expected value changed.
  - The five-by-five test is 25 cases plus a pin of both list lengths.
- Mutations I ran on `test_release_gate.py`, `test_generated_app.py` and `test_targets.py`:
  - `TARGET_ADAPTERS` back in the gate: 7 failed, 126 passed (`[docker.compose/v1-databricks]` and the six `[databricks-*]` cases), matching build.md.
  - Gate off: 40 failed, 93 passed.
- DECISIONS: the branch adds 9 rows and removes none. B-4085 corrects B-4070, whose own row is unedited. B-4086 is an extra new row the feedback did not ask for; it is accurate and edits no row. Neither id exists on 9e68ec2.

## 3. Migrations (scratch database, as the schema owner)
- Upgraded to main's heads (assist_0002, delivery_0007, packaging_0004, runtime_0009, others), took a catalog snapshot, then upgraded to heads (delivery_0008, packaging_0005).
- Tables with RLS flags, owner and grants (97), policies (117), functions with definers and their grants (75), column grants (408) and triggers (102) are identical before and after. Of 675 constraints, only the four named kind CHECKs change.
- `shifde_app` owns nothing. `delivery_targets`, `delivery_platform_claims`, `delivery_artifacts` and `solutions` stay enabled and forced.
- Downgrade to delivery_0007 and packaging_0004 restores main's catalog exactly; the second upgrade gives the same result as the first. Both files are unchanged since review 3.

## 4. Current main 9e68ec2
- `git merge-tree` with bbbe14d conflicts in docs/build/DECISIONS.md only, and that is also the only file both sides touch. Main's batch-4 adds no migration revision (`migrations/env.py` gains `hide_parameters` only).
- I merged it in the scratch worktree (DECISIONS left conflicted): mypy clean, ruff check and format clean, openapi snapshot matches, 2883 passed, 7 skipped, 0 failed.

## Findings
1. NOT BLOCKING, not isolation (front end, for the SP8 UI job): on 9e68ec2, `frontend/src/components/deliver/TargetsView.tsx:52-61` treats every non-Snowflake target as Databricks, and `delivery-types.ts` `PlatformKind` has two values. After this merge, a Fabric, Palantir or Docker target made through the API shows its raw kind and an empty Warehouse cell (or the Docker warehouse under Databricks columns). It shows only that client's own data. Fix: cells per platform and the five kinds in the type.
2. Carried over, unchanged, not reachable today: review 3 finding 2 (refusing default in `render`, #294, kept out by ruling) and finding 3 (`DockerBindings.workspace_host` not claim-checked, #294; nested Palantir suffixes, operator configuration, #292). SP8-9 must settle the claim check before adding a Docker pair to `PLATFORM_OF_KIND`.

## Commands
- Scratch worktree .worktrees/173-iso4 (detached at bbbe14d, since removed): `dev_db.py env`, `uv sync --frozen`: exit 0.
- At bbbe14d: `uv run --frozen pytest -q -n 16 tests/delivery tests/executor tests/packaging tests/identity/test_schema.py tests/schema tests/app/test_routes.py tests/authz/test_catalog.py`: 900 passed, 3 skipped (POSIX file modes).
- At bbbe14d: lint-imports 12 kept, mypy clean, openapi `--check` exit 0.
- On bbbe14d + 9e68ec2: `pytest -q -n 12 tests/delivery tests/executor tests/packaging tests/appgen tests/adapters/native tests/identity/test_schema.py tests/schema tests/board tests/app tests/core/test_hidden_parameters.py tests/authz/test_masked_ancestors.py tests/authz/test_catalog.py`: 2883 passed, 7 skipped, exit 0. An earlier, broader attempt hit my own 560 s limit without a result.
- Not run: full backend suite, front end. The scratch worktree's empty dev database on the local cluster was not dropped (`dev_db.py` has no drop command). The job worktree is clean at bbbe14d.
