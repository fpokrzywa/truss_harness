# Handoff — 2026-11-23 ~05:15 UTC, charter 7 factory run (lead session after 6975aeb6)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 8. Handed off at ~160k context, no agent and no gate running.

## Finished this session
- Salvage stash of session 6975aeb6 kept (four isolation reviews), B-3903. No worktree had uncommitted work.
- Merged: #190 (job 110, PR #222, hand merge; round-4 isolation review APPROVED in `reviews/110-isolation-r1.md`), #229 (job 118, PR #232, `job merge`).
- #224 = job 119: intake NEEDS-INFO (needs a lead migration: UPDATE grant on `jobs.payload` or a clearing function). Lead work, see step 5.
- Isolation reviews written: `reviews/109-isolation.md` (APPROVED), `reviews/111-isolation.md` (APPROVED, gap filed as #233). Both are on `lead/batch-2`, not on main yet.
- 099 (PR #227) sent back: `job feedback` round 3 (B-3906): convert `tests/connections/test_snowflake.py` invoke steps to the queued flow like dc2096f did for `test_adapter_hooks.py`.
- PR #209 (105) body changed `Closes #174` → `Refs #174`.
- **Next free lead id: B-3907.** Give new builders B-3161+ (118's builder had B-3151+ and used none).

## Exact next step
1. **Batch 2** (B-3904): branch `lead/batch-2` at `c86119e` (worktree `.worktrees/batch-2`) = origin/main `bedb370` + merges of 117 (#228), 105 (#209), 112 (#217), 111 (#213), 083 (#181), 084 (#187), 109 (#211) + lead docs (reviews 109/111, DECISIONS B-3904..B-3906). The `identity/oidc.py` conflict (084 vs 109) is hand-resolved: 084's `_load_jwks -> (lifetime, keys)`, one storing `_fetch_jwks`, 109's cache-free `run_checks` (`tests/identity` 274 passed, ruff, mypy OK). Main has since gained this handoff commit, so first `git -C .worktrees/batch-2 merge origin/main` (docs only; DECISIONS via `python /tmp/lead/resolve.py dec docs/build/DECISIONS.md` run from the worktree; STATUS/HANDOFF take main's side), push to `lead/batch-2` (no force), then `PYTHONUTF8=1 python scripts/gates.py --ref <sha> --name batch-2-merge` in the background (timeout 3600000; ~25 min). 13/13 (or only WinError 10055 setup errors that pass on rerun of those files) → `git -C .worktrees/batch-2 push origin HEAD:main` (fast-forward; GitHub marks the 7 PRs merged), then `factory/bin/job queue` moves those jobs to merged. A real failure: bisect by gating the parts.
2. **099 round 3**: spawn `builder` named `builder-099-cm-4-adapters-snowflake`, told it is resuming, with header + `Build round 3. Port: 6090.` + `Read feedback-1.md (lead decision B-3906) and do it; push and update build.md.` Then gate → reviewers (job.json) → approver → expect ESCALATE → lead checks only the converted test (isolation review of the adapter stands) → sync + `gates.py` → merge. After 099 merges: re-intake 102 (CM-7) with `job advance 102-cm-7-front-end intake`, resume 100 (CM-5, port 6100) as a resuming builder, then 103 (CM-8) after CM-1..7. #230 and #231 (Snowflake follow-ups) start only after 099 merges.
3. **087 (#144, PR #200)** alone after batch 2: real conflicts in `backend/openapi.json` (regenerate, don't hand-merge), `backend/tests/connections/test_routes_cross_client.py` and `backend/tests/runtime/test_worker.py` (keep both sides; never drop a cross-client case). Isolation review: `reviews/087-isolation-review.md`. Use the sync prompt in `/tmp/lead/sync_prompt.md` for a sonnet agent, or do it by hand.
4. New issues after batch 2 merges: #215 (needs 111), #218 (needs 112), #233 (needs 111). Start them through `factory/bin/job queue`.
5. Lead work on protected paths (migrations/schema tests): #174 items 1, 3, 4 (retention-delete guard hardening or a maintenance role, `deliberate_changes` reason for runtime_0002, evaluation pin); #224 migration for job 119 (then `job advance 119-clear-the-queued-request intake`); B-3081/B-3905 schema-test move onto `shifde.cli.catalog_rules` (removing nothing those tests check).

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 083-oidc-jwks-an-over | #139 | #181 | needs-human | batch 2 |
| 084-oidc-rate-limit-jwks | #140 | #187 | needs-human | batch 2 |
| 087-arch-m2-rest-2 | #144 | #200 | needs-human | step 3 |
| 099-cm-4-adapters-snowflake | #162 | #227 | build r3 | step 2 |
| 100-cm-5-live-tests | #163 | - | build r1, held | after 099 |
| 102-cm-7-front-end | #165 | - | needs-info (replied) | after 099 |
| 103-cm-8-scripts-scripts | #166 | - | needs-info (replied) | after CM-1..7 |
| 105-harden-retention-delete-guard | #174 | #209 | needs-human | batch 2 (#174 stays open) |
| 109-oidc-the-idp-test | #189 | #211 | needs-human | batch 2 |
| 111-backup-restore-check-restored | #197 | #213 | needs-human | batch 2 |
| 112-secrets-check-fail-when | #198 | #217 | needs-human | batch 2 |
| 117-find-and-fix-the | #226 | #228 | needs-human | batch 2 |
| 119-clear-the-queued-request | #224 | - | needs-info | step 5 migration |

## Traps learned this session
- In Git Bash, `/tmp/...` is not the same directory Python sees for an open() of `/tmp/...`: pass Bash paths as arguments (as `/tmp/lead/resolve.py dec <file>` does) or use a file inside the worktree.
- A batch built on another unmerged batch must be rebuilt if a member is dropped (reverting a merge would still leave the PR's head reachable from main and mark it merged). Rebuild from origin/main and reuse resolved files with `git checkout <old batch sha> -- <file>` for non-DECISIONS conflicts; `git diff --name-only <old> HEAD` vs the dropped PR's file list proves nothing else changed.
- `.worktrees/verify` is not left on the gated ref after `gates.py`; rerun failing files in the job's own worktree at the gated sha.
- 117's earlier merge gate run (`_gate-logs/117-find-and-fix-the-merge`) had stopped before pytest; it is covered by batch 2.
- Earlier traps (gates.py ~25 min, one at a time; `job merge` vs hand merge racing on main; `alembic heads` needs `code_heads()`; `cd` persists in Bash; mypy INTERNAL ERROR → delete `.mypy_cache`; eslint missing → `pnpm install --frozen-lockfile`) still hold.
