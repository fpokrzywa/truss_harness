# Handoff — 2026-11-15, end of session 6 (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Session 6 handed off at about 285k lead tokens (`scripts/context_size.py`): no agent running, no merge half done, `runtime` RT-1 blocked on an owner decision.

## Finished in session 6

All merged to `main` and pushed; `main` = `origin/main` at the commit carrying this handoff.

| Unit | Merged | Gate (clean checkout) | Review |
|---|---|---|---|
| models_admin test flake fix (B-1738) | `4101d40` | 8/8, 1228 passed | not critical |
| B-1914: pytest's default `norecursedirs` skipped `tests/build` in every full run; fixed + `tests/test_collection_layout.py` | `73e0be8` | 8/8, 1228 passed | n/a |
| build 2a (prompts, skills, executors) | `73e0be8` | 8/8, 1228 | not critical |
| build 2b (agents, tools, tool_runtime) | `786c8b1` | 8/8, 1261 | review 2 APPROVED |
| build 3a (pipeline_nodes, pipelines, catalog; ontologies/ontology_expr copied) | `1591807` | 8/8, 1343 | — |
| build 3b (orchestrations; ontologies/ontology_expr tests, B-1924 crash fix) | `6003a74` | 8/8, 1441 | — |
| build 4a (service.py + production `Env`, opus) | `56cf65d` | 8/8, 1598 | review 3 APPROVED |
| build 4b (api, kind_ops, ontology_ops) + `build_0002` (shared SHI models referable) | `be3fabc` | 8/8, 1812 in 558 s | review 4 BLOCKED, BLOCKED, APPROVED round 3 |
| **build done** | `2e03702` | | |
| ontology part 1 (`ontology_0001`, repository, engine, F-04 fixed) | `380d1dc` | 8/8, 1870 | ontology review 1 APPROVED |
| ontology part 2 (service, api) | `c58d282` | 8/8, 1910 in 648.6 s | ontology review 2 APPROVED |
| **ontology done** | `aa20033` | | |

Full backend suite on main: 1910 passed in 648.6 s (target 20 min). Front end not re-run this session (no front-end change since `4bcc1fb`).

## The exact next step

1. **runtime RT-1 is blocked on the owner** (STATUS "Waiting on owner input", B-1943). Branch `runtime` at `c43dcd9` (pushed; worktree `.worktrees/runtime`, db `shifde_runtime`). Its only failing gate is `tests/schema/test_rls_schema.py::test_stale_principal_table_entry_is_reported[shifde.runs-ported6-not a table in the database]`: the case hard-codes `shifde.runs` as a SETH table absent from the database, and `runtime_0001` creates it. Proposed fix: replace `"shifde.runs"` with `"shifde.installation_profile"` in that one parametrised case (B-1101: the one SETH table never ported). The permission system refused this edit for the builder, and refused the lead's earlier `PLAIN_FOREIGN_KEYS` edit. **Do not make it, or route around it, without the owner's explicit written approval.** If approved:
   1. Make the one-line edit on branch `runtime`, then run `python scripts/gates.py --ref runtime --workers 8 --no-frontend`.
   2. Run an isolation review of `aa20033..runtime`. Reviewer focus: `runtime_0001`, the three definers and their `test_function_schema.py` register entries, the two `global_tables.txt` lines (`workers`, `run_state_transitions`), the 13 `deliberate_changes.yaml` entries, the changed ledger/ontology tests (B-2106), and the C1 SQL tests.
   3. Merge to main and push.
2. **While waiting**, nothing in the layer order can start: RT-2..RT-12 build on RT-1's tables on the same branch, and `app`/`cli` need runtime. Work that does not depend on runtime and is allowed now:
   - Ontology review 2 G1 (group-subject policies through `authz.bound_groups`), as a small ontology follow-up unit.
   - Review 4 follow-up: the upgrade test asserts `trg_prompt_test_runs_append_only` is re-enabled.
   - Housekeeping below.
3. **After RT-1 merges**, use the runtime parts in `docs/build/inventory-runtime-ontology.md` section 2b:
   - Order: RT-2 schemas/pipeline_ops, RT-3 repository, RT-4 context/settle/gate, RT-5 ontology_exec/policies/schedules, RT-6 steps, RT-7 agent/action, RT-8 graph, RT-9 pipeline, RT-10 lifecycle, RT-11 service, RT-12 api/worker.
   - Builder: opus (`module-builder-critical`). The builder for RT-1 used about 342k tokens, so keep each part to one or two files.
   - Owed along the way:
     - C1 Python half and the end-to-end worker test (charter 5 item 3, before runtime is accepted);
     - the `run_of` contract (build review 3 F1, review 4 F3, ontology review 2 G2);
     - build review 2 F5;
     - the RT-3 composite keys for the bare id columns of B-2101;
     - a client-scoped due-schedule claim.
4. Then: `app`/`cli`/`main` (mount every router; about 150 deferred SETH tests by name in carry_forward; B-1625 before OIDC is mounted); the two-org proof (local cluster, S-002); the remaining front-end areas (B-1107); STATUS "Known defects" refresh; the factory install (charter §6).

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local and origin) | Up to date with this handoff commit |
| `runtime` (origin) | `c43dcd9`, RT-1, not merged (see above) |
| `build`, `ontology` (origin) | merged; done. Worktrees `.worktrees/build`, `.worktrees/ontology` can be removed (`git worktree remove --force`) |
| `.worktrees/verify`, `verify2`, `verify3` | Detached checkouts used by `gates.py --name` and reviewers (`shifde_verify3` is migrated to a review state; harmless) |
| Leftovers from session 5 | `.worktrees/fe-areas-1`, `secrets`, `skeleton`, `_scratch_admin`, `admin` (unregistered dir), `usage.txt`, `S:/tmp_identity_scratch`, `S:/tmp_identity_tools`, `S:/Development_2026/scratch_models_admin`: delete by hand |
| `observe/` (untracked, main checkout) | Not this build's; leave it |
| Local cluster | PostgreSQL 17 on 127.0.0.1:55450, running; scratch databases (`shifde_fix_rpm_flake`, `shifde_tmp_review_*`) harmless |
| Background agents | None running |

## Learned, not recorded elsewhere

- **The permission system guards `tests/schema/test_rls_schema.py`.** Any edit to it, even a reviewed exception-list entry or a stale example, was refused. Design around it (as `build_0002` did with `own_model_id`). Where no design avoids it, stop and record the item as waiting on the owner. Never ask an agent to make an edit you were refused.
- **pytest's default `norecursedirs` includes `build`** (B-1914). Any new test directory named like a default pattern would vanish silently; `tests/test_collection_layout.py` now guards this.
- **The gate pattern that worked:**
  - Merge the part into main locally (`--no-ff` if main moved).
  - `git branch -f main-gate HEAD`.
  - `gates.py --ref main-gate --name verify2 --workers 8 --no-frontend` in the background, with the review running in parallel.
  - Push only after both pass, then `git branch -D main-gate`.
  - Docs conflicts are unions; check duplicates with a `Counter`.
- **Resuming the same reviewer for later rounds is cheap** (2-15 min) and caught two real defects in `build_0002` round 2: the BYPASSRLS owner skipping a guard trigger via an early return, and a backfill blocked by an append-only trigger. A guard trigger must hold for the schema owner too.
- **Builders that write "proven to fail without the check" catch vacuous tests.** Keep asking for it in every brief.
- **Full suite times:** ~6-11 min on a quiet machine with `-n 8`. With a builder's full run going at the same time, they doubled; one builder saw 90 min at 39% with two leftover pytest processes. Check for stray `python.exe ... pytest` before blaming the code.
- **Agent sizes this session:**
  - sonnet builders 93-250k tokens per part (ontology part 1 252k, build 4b 247k);
  - opus RT-1 342k;
  - opus reviewers 94-154k.
- **Spend:** about $362 estimated at list price (sp1 $263.87 + lead $97.79) per `python scripts/usage_report.py`, up from about $285 at the end of session 5.
