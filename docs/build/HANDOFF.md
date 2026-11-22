# Handoff — 2026-11-22 ~16:40 UTC, charter 7 factory run (lead session 0017d47c)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 8 (B-2821). Handed off with context running low.

## Finished this session
- Merged: #177 job 078 (PR #179, main regression fixed), #129 job 075 (PR #178, hand-synced DECISIONS, then merged), #126 job 072 (PR #173, hand merge B-2822), #135 job 082 (PR #182), #142 job 086 (PR #186), #125 job 071 (PR #172, hand merge B-2824).
- Rejected by intake: #132 (already fixed on main, issue closed), #141 (job 085) and #148 (job 089): both need a lead-written migration. **Those two issues are still open and are lead work** (write the migration on a `lead/` branch, have `isolation-reviewer` read it, run `gates.py` 13/13, merge, close the issue).
- Filed: #183 (audit index `(tenant_id, correlation_id)`, lead migration), #185 (Windows `shifde-api` exits 3 on CTRL_BREAK), #188 (**flaky under load**: two `tests/runtime/test_worker_health.py` tests fail in the full suite, pass alone; this is why `gates.py` shows 12/13 now), #189 (OIDC test endpoint rewrites the shared discovery cache), #190 (connection invoke on the worker queue, needs a lead migration for a result table; B-2823).
- Decisions: B-2821..B-2824. Builders were told: 073 B-2831+, 078 B-2841+, 076 B-2851+, 080 B-2861+, 083 B-2871+, 082 B-2881+, 084 B-2891+, 086 B-2901+, 088 B-2911+. **Next free for the lead: B-2825**. Give builders B-2921+ (step by 10).

## Exact next step
0. **Update after the handoff:** 073's `gates.py` run finished at 12/13 on 30d9a08. The only failure is the #188 test `test_a_job_blocked_past_the_grace_does_not_hold_the_worker_up`. So step 2 is just: sync DECISIONS, hand merge PR #176, record a B- row. Job 088 (#147) was READY and has been moved to `gate`: run `factory/bin/gate 088-plan-approval-sibling-service`.
1. `PYTHONUTF8=1 factory/bin/job queue`.
2. `gates.py` run for 073 (`--name 073-sp4-4-cli-backup-lead`, ref 30d9a08) was running at handoff, with logs in `.worktrees/_gate-logs/073-sp4-4-cli-backup-lead/`. If it finished and its only failures are the #188 worker-health tests, hand merge PR #176 the way 071 was merged (B-2824): sync DECISIONS (keep both sides' rows sorted by id; `/tmp/lead/fixdec.py <file>` does it if it still exists), then `gh pr merge 176 --merge --match-head-commit <sha>`, and record a B- row. Otherwise rerun it.
3. Then hand merges in this order, each with a `gates.py` run (one at a time, a single `.worktrees/verify`): 080 (PR #180), 083 (PR #181), 084 (PR #187, after 083; both touch identity/oidc.py), 076 (PR #184). Isolation reviews are done and saved for 080, 083 and 084 (`docs/build/reviews/`). 076 changes only scripts/docs: no isolation review is needed, `gates.py` alone is enough.
4. Fix #188 soon: make it a job right away. Until it is fixed every full suite shows 12/13.
5. Owner priority S-025: `lead/cm-l1-l2` (see the previous HANDOFF in git history: `git show ecfb2ef:docs/build/HANDOFF.md`). Merge origin/main into it, run `gates.py`, merge, then CM-1 (#159) onward.

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 073-sp4-4-cli-backup | #127 | #176 | needs-human | Step 2 above. Isolation reviews 1 and 2 APPROVED. |
| 074-sp4-5-cli-backup | #128 | - | build r1, not started | After 073 merges: spawn the builder (port 5840). |
| 076-sp4-7-scripts-procctl | #130 | #184 | needs-human (scripts/) | `gates.py` then hand merge. |
| 077-sp4-9-go-live | #131 | - | build r1, not started (spec done) | After 073, 074 and 076 merge (port 5870). |
| 080-model-check-job-result | #133 | #180 | needs-human (audit/) | `gates.py` then hand merge. |
| 081-arch-m2-follow-up | #134 | - | build r1, not started (spec done) | After 080 merges (same lookup; port 5910). |
| 083-oidc-jwks-an-over | #139 | #181 | needs-human (identity/) | `gates.py` then hand merge. |
| 084-oidc-rate-limit-jwks | #140 | #187 | needs-human (identity/) | After 083: sync, `gates.py`, hand merge. |
| 087-arch-m2-rest-2 | #144 | - | build r1, not started (spec rewritten under B-2823) | After 080 merges (port 5970). |
| 088-plan-approval-sibling-service | #147 | ? | build r1 (builder was running) | Check `head -1 build.md`. If READY: `job advance 088... gate` (port 5980). |
| new | #155, #156, #157 + the rest held | | | `job create` as slots free (#156 is the connection space epic, its slices are #159-#166). |

## Traps learned this session
- Never `cd` into a job worktree in the session shell. A background command started from there keeps the folder open, and then `job queue` fails with WinError 32 when it removes the worktree. Use `(cd .worktrees/<id> && ...)` subshells.
- `job merge` stops at needs-human on any DECISIONS.md conflict. Resolve it in the worktree (keep both sides, rows sorted), push, rerun the checks, then `gh pr merge --match-head-commit`.
- Approvers escalate any change under identity/, audit/, scripts/, core/crypto.py, the proxy or migrations. Plan a lead isolation review plus `gates.py` for each one.
