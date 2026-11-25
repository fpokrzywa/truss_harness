# Handoff — 2026-11-24 19:20 EST, charter 7 factory run (lead session 3899de12), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running; no agent or gate running at handoff. Merged this session: 178, 175, 174, 172 (STATUS.md and B-4036). Main is `6aad634` plus docs commits; CI on main `6aad634` had not finished when this was written.

## Rulings (supervising session is `agt-trus-harness-ad`, f18944f0; send ruling requests there)
- S-041 (176) stands with its conditions; #361 is now on main. S-042 and S-043 are used.
- 173: held. Needs a fresh ruling (S-044) on its synced head after full gates and green CI. S-040 does not cover it.
- The supervisor re-runs the hosted DB setup after 173 and after 176, with no gate active: tell it when each merges and wait for its "done" before starting the next full gate.
- Merge only with green CI on the head that merges: sync and push first, wait for CI, then `job merge` (B-4036; its own sync otherwise makes a new head).
- Never skip, loosen or add retries to the C1 and job-kind tests. A red CI is real until explained.
- If `scripts/supervisor/autostart.ps1` shows as deleted, neither commit the deletion nor restore it; tell the supervisor.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`. It should move 175 and 172 to `merged`. It failed for this session with WinError 32 because this session's shell sat in the empty `.worktrees/175-sp8-3-mcp-tools`; if it still fails, check what holds that folder.
2. **179** (#363, PR #365, approve round 1, `decision.md` APPROVE): sync onto main (DECISIONS.md may conflict: keep both sides' rows), push, wait for green CI on that head, then `job merge 179-make-a-stuck-reaper`. CI was red on `7ce98766` (`test_orphaned_holds.py`, #366). If that test fails again away from midnight UTC, stop and tell the supervisor. Record the cause in 179's DECISIONS row.
3. **176** (#351, PR #356) per S-041: sync (DECISIONS.md conflicts), full `176-merge` gates, green CI on that head, hand merge with `gh pr merge 356 --merge --match-head-commit <sha>`. Tell the supervisor.
4. **173** (#286, PR #354): sync, full `173-merge` gates, green CI, then ask for S-044.
5. **177** (#359, intake ACCEPT; 175 is merged): `route`, `job advance ... spec`, spec-writer.
6. Issues whose dependency is now merged: #273 and #358 (needed #272), #360 (needed #288; its item 4, the runbook line, comes before any deployment that enables `assist_ask`), #284 (needed #283), #366 (tests only). Create them with the `/factory run <n>` path while other `new` lines are held.

Next free lead decision id: **B-4037**. Given out: 179 B-4031..B-4035.

## Open jobs (stage)
173 needs-human (held), 176 needs-human (S-041), 177 intake (ACCEPT), 179 approve r1 (APPROVE, CI red). 172 and 175 are merged on GitHub; their job.json still says needs-human.

## Dependencies (job queue does not know them)
#274/#275 after #273 (#275 has two lead comments: Snowpark column-table re-check, and the version DEEP_HEALTH resolves to, S-043); #290, #292, #294, #352 need #286; #364 needs #351 (new migrations); #295, #296 last. #358 before any live Snowflake app. When SP6, SP7 and SP8 finish, run each done-when demo and record it in STATUS.md.

## Traps learned
- Do not `cd` into a job worktree: while a shell's directory is inside it the factory cannot remove it, and a bare `git` in the emptied folder acts on the main checkout. Use `git -C <path>` and absolute script paths.
- `gates.py` output is buffered when redirected: run it with `python -u`, or read `.worktrees/_gate-logs/<name>/`.
- The gate inside `job merge` writes `round-N/merge-checks.md`, not `checks.md`.
- CI runs on main showed `cancelled` when a newer main push followed.
- CI `backend` took 25 to 38 minutes; every sync that moves the head restarts it. Push docs to main before syncing a branch.
- Wait for the builder's completion notification before `factory/bin/gate` (its stop hook runs `checks.sh` in the same worktree).
- TaskStop on a gate leaves its children: `taskkill /F /T /PID` only the tree of your own command, after re-checking its command line.
- One full gate run at a time (about 27 minutes); one factory gate alongside it was fine. Run a long gate as the lead's own background Bash task.
- Load flakes (`[Errno 10048]`, WinError 10055): a targeted rerun passes (B-3984). Full gates catch stale pinned expectations `checks.sh` skips (`tests/authz/test_catalog.py`, `tests/app/test_routes.py`, `tests/identity/test_schema.py` heads).
- `find .` from the repo root times out. A wait loop that greps process command lines matches itself. `uv run alembic heads` does not work from `backend/`.
- If a factory gate reports `'eslint' is not recognized`, run `pnpm install --frozen-lockfile` in that worktree's `frontend` and rerun it.
- Earlier traps hold: PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, full job ids only, `factory/bin/job` run directly, worktree scripts via `uv run --frozen --project backend python ...`, DECISIONS id ranges per builder, isolation-reviewer is read-only (the lead saves its review), approver escalates any new migration by design. Startup reads cost about 90k tokens: use `cut` on STATUS.md lines and `python scripts/context_size.py` early.
