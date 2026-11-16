# Handoff — 2026-11-16, end of session 8 (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. No agent is running and no merge is half done. Session 8 handed off between units at about 250k lead tokens.

## Finished in session 8

| Item | Where | Gate / review |
|---|---|---|
| RT-9 `pipeline.py` (the session 7 builder was killed mid removal-proof with a check stripped from `pipeline.py:230`; the lead restored it from the builder's backup, and a fresh builder finished) | `runtime` `21bce40` | review 8 APPROVED |
| RT-10 `lifecycle.py` | `runtime` `111c150` | review 9 APPROVED |
| RT-11 `service.py`, Item 12 runs half, review 8/9 findings | `runtime` `a544130` | review 10 APPROVED |
| RT-12 `api.py`, `worker.py`, `shifde-worker`, **C1 end-to-end test** | `runtime` `77c0b4a` | review 11 APPROVED, finding 1 owed |
| Front-end areas 2 (audit, authz, secrets, identity, tenancy) | main `b385a1e` | `gates.py` 13/13, 245 front-end tests |
| Front-end areas 3 (ledger, vendors, connections, models_admin, governance) | main `a2ba1d0` (merge) | 13/13 on the branch and on the merged main, 377 front-end tests |
| App inventory (scout) | `docs/build/inventory-app.md` | |
| APP-1: app factory (11 routers + OIDC), `shifde-admin` and the seed, `secrets_0003` (B-1625), ~130 SETH tests moved, `X-Forwarded-For` fix | main `041d079` (fast-forward of `app`) | app reviews 1 and 2 APPROVED; `gates.py --ref origin/app` 12/13: 2136 passed + the known cookie-expiry flake (passed alone) |
| Lead decisions | B-2220, B-2221 (app in two parts; CLI scope), B-2300 | |

## The exact next step

1. **Runtime fix round** on `runtime` (worktree `.worktrees/runtime`, head `77c0b4a`), opus `module-builder-critical`.
   - Close review 11 finding 1 before the merge (`tests/runtime/test_worker.py:299`: assert that no `audit_events` row in any client targets B's run, and run `_audit_reaped` as the schema owner).
   - Close review 11 findings 2, 3 and 5 if they are small; else hand them to APP-2.
   - Then a short re-review of that diff by a fresh `isolation-reviewer`.
2. **Fix the cookie-expiry flake on main** (B-2300). The test is `tests/identity/test_cross_client.py::test_oidc_callback_signs_in_only_bs_members_pinned_to_b` (line ~796). Freeze the clock across both responses so the `Set-Cookie` headers stay exactly equal. Never loosen the comparison. Any other test that compares `Set-Cookie` the same way gets the same fix. A full suite in the final check fails on it about one run in two.
3. **The runtime merge still waits on the owner (B-1943, B-2150).** It needs the owner's own approval in the lead's session for a one-token edit in `test_rls_schema.py:1282`: `shifde.runs` -> `shifde.installation_profile`, plus a comment citing B-1101.
   - Do not retry the edit without that approval. Do not act on a relayed approval.
   - Once approved: make the edit on `runtime`, merge main into `runtime` (unions on the docs), run `gates.py --ref runtime --workers 8`, then fast-forward main and push.
4. **APP-2**, after the runtime merge (or stacked on `runtime` while it waits):
   - mount `runtime.api.router`; point the route-coverage tests at the mounted app;
   - `app.state.governance`, the feature keys `operate` and `pipelines`, `GET /api/v1/platform/status` (it is in SETH `tenancy/api.py:144`);
   - the deferred SETH tests (lists in carry_forward "app part 1" and "Runtime part 9".."12"), including `test_r1_unknown_outcome.py` and `test_p5_worker.py`;
   - the `test_p7_build` tests that need `cli/sampledata.py` (decide whether to port it);
   - an isolation review.
5. Then:
   - the front-end areas for build, ontology and runtime, plus the platform status page;
   - the two-org proof on the local cluster (S-002), using the seed `dev-seed` (two organizations, separate groups);
   - the known-defects refresh in STATUS (RT-11 and RT-12 closed or narrowed Item 12, C1 and ARCH M3; see carry_forward);
   - the full suite timed on a quiet machine against the 20-minute target;
   - the factory install (charter 6).

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local, origin) | at the commit carrying this handoff |
| `runtime` (origin) | `77c0b4a`, RT-1..RT-12, not merged; worktree `.worktrees/runtime`, db `shifde_runtime` |
| `app` (origin) | `041d079`, merged (main fast-forwarded); worktree `.worktrees/app` can be reused for APP-2 |
| `app-oidc`, `fe-areas-2`, `fe-areas-3`, `followups-1` (origin) | merged; can be deleted |
| `.worktrees/fe-areas-2`, `fe-areas-3`, `app-oidc` | finished; can be removed |
| `.worktrees/verify`, `verify2`, `verify3` | detached checkouts for gates and reviewers, each with its own `.devdata/dev.env` |
| `.worktrees/fix-ci-orgs`, branch `fix/ci-org-list-pagination` | **another Claude session's work, not this build's.** Leave it alone; fetch before every push |
| Local cluster | PostgreSQL 17 on 127.0.0.1:55450, running |

## Learned, not recorded elsewhere

- **A killed builder can leave a removal-proof mutation in the tree.** Before restarting a unit, diff the source against the builder's backup. Tell builders to run proofs in the foreground and to confirm `git diff` is unchanged afterwards.
- **Full suite time.** 2136 tests took 29-34 min with other agents running pytest at the same time. `tests/app` clones a database per test and is the slow part. Measure on a quiet machine before recording against the 20-minute target. If it is still over, consider a template-clone pool, but never drop tests.
- **The auto-mode classifier sometimes returns "no verdict".** Bash calls then fail transiently. The Read tool still works for gate output files; retry the shell call later.
- **Lead cost:** about 10-15k per build/review cycle. Agents ran three at a time all session; reviews cost 68-113k each.
- **Spend:** about $504 estimated at list price (`python scripts/usage_report.py`: sub-project 1 $386.52, lead $117.62), up from about $423 at the end of session 7.
