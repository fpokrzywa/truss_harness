# Handoff — 2026-11-23 ~13:45 UTC, charter 7 factory run (lead session 2335cf47)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (sub-projects table, SP4 and SP5 rows), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. No agent running.

## Finished this session
- `golive_check.py --local` on `76e9b72`: `GO-LIVE CHECK PASSED (local)` (B-3916). SP4 row updated: met locally; four register rows wait on the owner's written acceptance (Item 20 audit-retention, Item 23, REQ F-08, REQ F-22); `--final` needs a `Hosting target` register row (owner).
- SP5 short design written, builder-read (32 guesses) and settled (B-3917): `docs/superpowers/specs/2026-11-23-subproject-5-solution-generator-databricks-design.md`. Cut into #254-#262 (SP5-1..9, `sp5`).
- Merged: job 130 #253 (PR #263, `100282d`, B-3918), job 131 SP5-1 #254 (PR #264, `91ff610`, B-3919).
- Filed: #265 (job 132), #267 (runtime read-only check, first keyword only; pre-existing).
- **Next free lead id: B-3920.** Builder ids: next free B-33xx after the highest in DECISIONS (B-3322 used by job 132).

## Open jobs
- **132-retry-winerror-10055-on** (#265, PR #266): stage `approve`, decision APPROVE. It changes `backend/tests/_support` and `conftest.py`, so (supervisor) run `python scripts/gates.py --ref <synced head> --name 132-merge` first; if 13/13 (or only known load flakes that pass alone), `factory/bin/job merge 132-retry-winerror-10055-on`.
- Job 131's worktree dir `.worktrees/131-sp5-1-sql-tools` may still be held open by a process; if `factory/bin/job queue` raises WinError 32 on it, close the holder (or reboot shell) and rerun `job queue` so job 131 moves to `merged`.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`. Start `new` lines in SP5 dependency order only: #255 SP5-2 next (depends on #254, merged); #257 SP5-4 after #255; #256 after #255; etc. (dependencies in each issue body). Also start #267 (independent). Do not start an SP5 issue before its dependencies merge.
2. Job 132 as above.
3. SP5-4 (#257) needs the lead to add the appgen-web lines to the protected `factory/checks.sh` by hand.
4. Lead work still open: #174 items 1, 3, 4 (protected paths: maintenance-role/guard migration, `runtime_0002` deliberate_changes reason, stuck evaluation pin); #147 part 2 (API-key creator relation, `delivery_approvals` trigger migration); B-3081/B-3905 schema-test move onto `shifde.cli.catalog_rules`. Stale STATUS known-defect rows to refresh: REQ F-03 (merged PR #29), Item 20 (delete closed by #126), Item 21 (closed by #47/#144).
5. Then SP6-SP8 short designs (charter 7 step 1).

## Traps learned
- Isolation-critical SP5 jobs escalate (approver) on vendor adapter changes; lead does isolation-reviewer + `gates.py` + hand merge (`gh pr merge --match-head-commit`). The `isolation-reviewer` is read-only: the lead writes its report into `docs/build/reviews/`.
- Don't start `gates.py`/`checks.sh` from a shell whose cwd is a job worktree: `job queue` then cannot remove that worktree (WinError 32).
- Running a `checks.sh` gate alongside a full `gates.py` suite produced one load flake (HangServer wait); rerun alone at `-n 8` before calling a failure real.
- Earlier traps still hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, DECISIONS append conflicts: keep both sides, main's first).
