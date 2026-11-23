# Handoff — 2026-11-23 ~07:15 UTC, charter 7 factory run (lead session ca60ae91)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit (`9f65444` + docs). Cluster on 55450 running. `concurrency` 8. Handed off at ~175k context, no agent and no gate running.

## Finished this session
- Batch 2 merged (fast-forward `a2b2869`): 117, 105, 112, 111, 083, 084, 109. 13/13 on the rerun.
- 099 CM-4 Snowflake merged by hand `1b2843d` (B-3907).
- 122 (#233) merged through `job merge` (`9f65444`).
- Lead migration `runtime_0007` for #224: branch `lead/jobs-payload-scrub` (`bc4c6eb`, worktree `.worktrees/lead-jobs-payload`), adds `GRANT UPDATE (payload)` on `shifde.jobs` plus guard trigger `trg_jobs_payload_guard` (only `connection.invoke`, status done/dead, `NEW.payload = OLD.payload - 'request' - 'write'`). Isolation review APPROVED (`docs/build/reviews/runtime_0007-isolation.md`). Targeted tests and lints pass; NOT yet full-gated or merged.
- Filed #234 (timing race in two approval tests, = job 123), #235 (rest of restore catalog rules, follow-up of 122).
- **Next free lead id: B-3908.** Builder decision ids used: B-3171 (120), B-3181 (121). Give new builders B-3201+.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue` (always with `PYTHONUTF8=1`: `job queue`, `gate` crash on cp1252 otherwise) and start every `resume`/`new` line:
   - 123 at **gate**: `PYTHONUTF8=1 factory/bin/gate 123-fix-the-timing-race`, then reviewers (code sonnet), approver.
   - 120 at **approve**: spawn `approver` (PR #239, final round 1). Then `job merge` (it will sync against 122, which touched the same files; resolve keeping both rules).
   - 100 (CM-5, port 6100, resuming builder), 102 (CM-7: `job advance 102-cm-7-front-end intake`), 103 (CM-8) only after CM-1..7, and new #230, #231, #235, #238 now that 099 is in.
2. 121 (#218, PR #236) at needs-human: approver escalated only for `secrets/` path; gate + code + security PASS. Lead: isolation review of the diff, sync with origin/main, `gates.py --name 121-merge`, hand merge on 13/13 (or only WinError 10055 setup errors that pass on rerun).
3. runtime_0007: merge origin/main into `.worktrees/lead-jobs-payload`, add a DECISIONS row (B-3908), `gates.py --name runtime-0007-merge`, fast-forward main on 13/13. Then `PYTHONUTF8=1 factory/bin/job advance 119-clear-the-queued-request intake` and answer intake: the migration is `runtime_0007`, the code clears `payload.request`/`payload.write` inside the worker's per-job organization context (isolation note 10).
4. 087 (#144, PR #200) alone: conflicts in `backend/openapi.json` (regenerate), `tests/connections/test_routes_cross_client.py`, `tests/runtime/test_worker.py` (keep both sides). Isolation review `reviews/087-isolation-review.md`. Sync prompt `/tmp/lead/sync_prompt.md`.
5. Lead work on protected paths still open: #174 items 1, 3, 4; B-3081/B-3905 schema-test move onto `shifde.cli.catalog_rules`.

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 087-arch-m2-rest-2 | #144 | #200 | needs-human | step 4 |
| 100-cm-5-live-tests | #163 | - | build r1, held | resume now |
| 102-cm-7-front-end | #165 | - | needs-info (replied) | resume now |
| 103-cm-8-scripts-scripts | #166 | - | needs-info (replied) | after CM-1..7 |
| 119-clear-the-queued-request | #224 | - | needs-info | step 3 |
| 120-restore-check-pin-bodies | #215 | #239 | approve r1 | step 1 |
| 121-refuse-rewrap-audit-check | #218 | #236 | needs-human | step 2 |
| 123-fix-the-timing-race | #234 | #240 | gate r1 | step 1 |

## Traps learned this session
- `factory/bin/gate` and `job queue` die with UnicodeEncode/DecodeError under cp1252: always set `PYTHONUTF8=1`.
- uv in the main checkout fails with os error 396 (hardlinks on a cloud file) unless `UV_LINK_MODE=copy` is exported.
- WinError 10055 setup errors recur under load (supervisor: not port exhaustion; #238 asks for a test-harness connect retry). Rerun the failing file at the gated sha before deciding.
- The two approval-test races (#234) can fail a full suite; they pass on rerun. Job 123 fixes them.
- Earlier traps (gates.py ~25 min, one at a time; `/tmp` differs between Bash and Python; `.worktrees/verify` not left on the gated ref; mypy INTERNAL ERROR → delete `.mypy_cache`) still hold.
