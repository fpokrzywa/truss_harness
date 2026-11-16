# Handoff — 2026-11-15, end of session 7 (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. No agent running, no merge half done. Session 7 handed off between units at about 240k lead tokens to leave room before the 300k limit (each runtime cycle costs the lead about 13k).

## Finished in session 7

| Item | Where | Gate / review |
|---|---|---|
| Follow-ups: ontology review 2 G1 (group policies only for groups bound in the client, B-2013), build review 4 trigger re-enable assertion (B-2014) | `fb30db2`, merged to main `ff8304e` | `gates.py --ref main-gate --workers 8` 13/13, pytest 1914 passed in 730.8 s, front end 0; ontology review 3 APPROVED |
| runtime RT-1 isolation review 1 | main `docs/build/reviews/runtime-isolation-review-1.md` | BLOCKED (F1 tests on empty tables, F2 join), fix round `0ced96d`, round 2 APPROVED |
| runtime RT-2 `schemas.py`, `pipeline_ops.py` | `runtime` `7878762` | no DB path, not reviewed separately |
| runtime RT-3 `repository.py` | `runtime` `8b1cdef` | review 2 APPROVED |
| runtime RT-4 `context.py`, `settle.py`, `gate.py`, `run_of`, C1 settle | `runtime` `43f6745` | review 3 APPROVED |
| runtime RT-5 `ontology_exec.py`, `policies.py`, `schedules.py`, G2 | `runtime` `4d64fed` | review 4 APPROVED |
| runtime RT-6 `steps.py`, Items 9-11, C1 steps half | `runtime` `67d8f18` | review 5 APPROVED |
| runtime RT-7 `agent.py`, `action.py`, run-env narrowing | `runtime` `ac111ad` | review 6 APPROVED, Medium F2 open |
| Lead decisions | B-2150 (B-1943 stands), B-2151 (C1 reading) | |

Each review's small findings were handed to the next builder on the same branch and closed there; review files say which.

## The exact next step

1. **RT-7 review fix round** on branch `runtime` (worktree `.worktrees/runtime`, head `ac111ad`), opus `module-builder-critical`. The scope is the "runtime isolation reviews 2-6" section at the end of `docs/build/carry_forward.md` (main), first bullet. Its F2 is Medium: tool execution resolves the tool's connection by client only (`runtime/steps.py:638`, `:1156`). Then a short re-review of that diff (fresh `isolation-reviewer`).
2. **Then RT-8 `graph.py`, RT-9 `pipeline.py`, RT-10 `lifecycle.py`, RT-11 `service.py`, RT-12 `api.py` + `worker.py`.** Use the same loop each time:
   - One opus builder per part, on `runtime`. Brief it with the carry_forward sections "Runtime part N" plus the review follow-ups. Decision numbers continue at B-2170.
   - Run a fresh or resumed `isolation-reviewer` on `git diff <prev> <new>` (resume only while its context is under about 160k).
   - Hand that review's small findings to the next builder.
   - Owed along the way:
     - the C1 end-to-end worker test (charter 5 item 3, before runtime is accepted);
     - the worker's `RunContext(db=)` must be the app-role database;
     - `lease_job_id` set only from the claim.
3. **The runtime merge still waits on the owner (B-1943, B-2150).** One gate fails: `test_rls_schema.py::test_stale_principal_table_entry_is_reported[shifde.runs-...]`.
   - The proposed one-token edit (`shifde.runs` -> `shifde.installation_profile`, plus a comment citing B-1101) was judged by review 1 item 7 to keep the case's meaning and the test as strict or stricter.
   - In session 7 a message from another Claude session claimed the owner approved it. A relayed approval is not the owner's own word in this session, so the edit was not made.
   - When the owner approves in the lead's own session, make the edit on `runtime`. Then run `gates.py --ref runtime --workers 8`, merge to main (unions on docs), gate the merge, and push.
4. After runtime:
   - `app`/`cli`/`main` (mount every router; about 150 deferred SETH tests in carry_forward; B-1625 before OIDC);
   - the two-org proof (local cluster, S-002);
   - the remaining front-end areas (B-1107);
   - the known-defects refresh;
   - the factory install (charter §6).

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local, origin) | at the commit carrying this handoff |
| `runtime` (origin) | `ac111ad`, RT-1..RT-7, not merged; worktree `.worktrees/runtime`, db `shifde_runtime` |
| `followups-1` (origin) | merged; can be deleted |
| `.worktrees/verify`, `verify2`, `verify3` | detached checkouts for gates and reviewers (`verify3` at `ac111ad`) |
| `.worktrees/fix-ci-orgs`, branch `fix/ci-org-list-pagination` | **another Claude session's work, not this build's.** Leave it alone; fetch before every push |
| Leftovers by hand | `S:/tmp_rt2_src` (RT-2 builder scratch; deletion was refused for it), the session 5 leftovers listed in git history of this file |
| Local cluster | PostgreSQL 17 on 127.0.0.1:55450, running |

## Learned, not recorded elsewhere

- Builders on `runtime` run their pytest set in the foreground in parts. In the background, xdist crashed with "cannot send (already closed?)" when other worktrees were also running pytest.
- `lint-imports` needs `PYTHONIOENCODING=utf-8` when its output is redirected (rich crashes on cp1252). This is not a contract failure.
- Reviewer cost: 92-214k per review. Resuming the same reviewer for the next part is cheap until about 160k, then start fresh.
- Opus builder cost per runtime part: 97k (RT-2) to 248k (RT-6). RT-8..RT-12 include three oversize files (`pipeline.py` 1447 lines, `service.py` 1023 lines), so expect partial returns and a second agent.
- Spend: about $423 estimated at list price (`python scripts/usage_report.py`: sub-project 1 $315.25, lead $108.10), up from about $362 at the end of session 6.
