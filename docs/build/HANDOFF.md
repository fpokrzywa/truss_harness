# Handoff — 2026-11-23 ~08:40 UTC, charter 7 factory run (lead session c88d27a4)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit (`7497df9` + docs). Cluster on 55450 running. `concurrency` 8. Handed off at ~185k context. No gate or merge running. Builders for 125 and 127 had finished (or were waited for) before this handoff; see the job table.

## Finished this session
- Merged: 123 (#234, PR #240, hand merge `9431630`, B-3909), 124 (#230, PR #245, `job merge`), 100 CM-5 (#163, PR #242, `job merge`), 120 (#215, PR #239, `7497df9`: `job merge` synced and gated PASS, then only `gh pr merge` failed with an empty error; lead ran `gh pr merge --match-head-commit 91fecb0`, B-3910).
- CM-7 (102, #165) re-queued (CM-1..4, CM-6 on main), specced, built, gate PASS, code/ux/ui/security PASS. At approve.
- 126 (#235) intake (rescoped: principal-table check without the SETH snapshot), spec, build, gate PASS, code+security PASS. At approve.
- 125 (#231) round 1 approver ESCALATE (security review found `WITH "p\" AS PROCEDURE ... CALL "p\"()` bypass: tokenizer honours `\"` in double quotes, Snowflake does not). Lead feedback-1 (B-3212): fix it in this PR (Snowflake dialect: no backslash escape in "...", no backtick quoting, `--`/`//` end at `\r`, tests, correct B-3211 wording). Round 2 builder spawned.
- 127 (#238, test-harness connect retry on WinError 10055/10048) intake, spec, build round 1.
- Filed #241 (AdapterOut auth-only/destination fields; drop CM-7's `frontend/src/lib/adapter-fields.ts` copy; build after #165 merges).
- runtime_0007 (`lead/jobs-payload-scrub`): origin/main merged in, B-3908 row, pushed `aa5b33c`. `gates.py --name runtime-0007-merge` 12/13: full suite 4838 passed, 3 errors, all psycopg ConnectionTimeout at connect (test_p7_query, build/test_routes_cross_client, runtime/test_graph) while job 127's builder ran its own pytest; supervisor confirms load, not the migration. Main has moved since, so it needs re-sync and re-gate.
- **Next free lead id: B-3911.** Builder ids used: B-3211/B-3212 (125), B-3221 (124), B-3231 (127), B-3241 (126), B-3251 (102). Give new builders B-3261+.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue` and start every `resume`/`new` line:
   - 102 at **approve**: spawn `approver` (PR #246, final round 1), then `job merge`.
   - 126 at **approve**: spawn `approver` (PR #247, final round 1), then `job merge` (it touches the same restore files as 120; sync keeps both).
   - 125 at **build r2** (or gate if its builder finished READY): gate, then code+security (opus), approver.
   - 127 at **build r1** (or gate): gate, code (sonnet), approver. After it merges, WinError 10055 should stop failing gates.
   - #241 only after 102 merges.
2. runtime_0007: in `.worktrees/lead-jobs-payload` merge origin/main, push, `gates.py --name runtime-0007-merge2` when no builder runs pytest; on 13/13 (or only connect errors that pass on rerun) push HEAD:main (fast-forward). Then answer #224 and `job advance 119-clear-the-queued-request intake`.
3. 103 (CM-8, #166) only after CM-7 (102) merges: comment on #166 and `job advance 103-cm-8-scripts-scripts intake`.
4. 121 (#218, PR #236) needs-human: lead isolation review, sync, `gates.py --name 121-merge`, hand merge.
5. 087 (#144, PR #200) alone: conflicts in `backend/openapi.json` (regenerate), `tests/connections/test_routes_cross_client.py`, `tests/runtime/test_worker.py` (keep both). Isolation review `reviews/087-isolation-review.md`.
6. Lead work on protected paths still open: #174 items 1, 3, 4; B-3081/B-3905 schema-test move onto `shifde.cli.catalog_rules`.

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 087-arch-m2-rest-2 | #144 | #200 | needs-human | step 5 |
| 102-cm-7-front-end | #165 | #246 | approve r1 | step 1 |
| 103-cm-8-scripts-scripts | #166 | - | needs-info (replied: wait) | step 3 |
| 119-clear-the-queued-request | #224 | - | needs-info (replied: wait) | step 2 |
| 121-refuse-rewrap-audit-check | #218 | #236 | needs-human | step 4 |
| 125-harden-check-read-only | #231 | #244 | build r2 | step 1 |
| 126-restore-catalog-rules-port | #235 | #247 | approve r1 | step 1 |
| 127-intermittent-winerror-10055-at | #238 | - | build r1 | step 1 |

## Traps learned this session
- 103 and 119 show as `resume` (author replied) but the replies say wait; they take the free-slot lines and push new issues to `hold`. `job create <n>` still works when a running slot is free.
- `job merge` can end `needs-human: The merge failed:` with an empty reason after the sync gate PASSED and the push succeeded; check `gh pr view <n> --json mergeable,headRefOid` and finish with `gh pr merge --match-head-commit <sha>`.
- `job merge`'s sync gate can fail on one WinError 10055 setup error: grep `^(FAILED|ERROR) ` in `round-N/merge-checks.log`, rerun those files at the synced sha in the job worktree, then push (fast-forward) and hand merge with a DECISIONS row.
- Reviewers sometimes reply with a verdict without writing `round-N/review-<r>.md`; say "write it to round-N/review-<r>.md before replying" in the prompt.
- Builders can be reported "stalled" by the watchdog after they already finished; check `build.md` first line and `git status` before respawning.
- Don't run gates.py while a builder runs its own full pytest (load → connect timeouts). Earlier traps (PYTHONUTF8=1, UV_LINK_MODE=copy, gates.py ~25-40 min one at a time) still hold.
