# Handoff — 2026-11-24 17:50 EST, charter 7 factory run (lead session b10341d0), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running; no agent or gate running at handoff. Nothing was merged this session.

## Rulings (supervising session is now `agt-trus-harness-19`; send ruling requests there)
- S-041 (176) and S-042 (175) are in `docs/build/DECISIONS.md` with their conditions; read the rows.
- 173: held. S-040 no longer covers it (15:42). After 178 merges: sync, full gates, green CI, then send the new head for a fresh ruling.
- 174: merge through `job merge` only with green CI.
- 178: not ruled; the supervisor read it and saw no blocker. If its approver escalates, send the decision once CI `backend` on `d01e6732` has finished. It goes first.
- Never skip, loosen or add retries to the C1 and job-kind tests.
- If `scripts/supervisor/autostart.ps1` shows as deleted, neither commit the deletion nor restore it; tell the supervisor.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`.
2. **178** (#361, PR #362, approve, round 1): `gh pr checks 362`; spawn `approver` with the header and `PR: #362. Final round: 1.`; give it `docs/build/reviews/178-isolation-review-1.md` (APPROVED). The diff is runtime product code with no migration.
3. **172** (#272, PR #353, approve, round 3): spawn `approver` (`Final round: 3.`). Isolation review 2 APPROVED was at `c98948c`; round 3's diff has had no isolation look (code and security PASS): spawn `isolation-reviewer` for `c98948c..d32183b`. The PR body still says Snowpark is pinned in the dev group: edit it (`gh pr edit 353`).
4. **175** (#288, PR #357): full `175-merge` gates and green CI on one head (S-042); does not wait for 178. Then 177.
5. After 178 is on main: **174** sync, CI green, `job merge`; **176** per S-041 (sync, `176-merge` gates, green CI on that head); **173** sync, gates, CI, fresh ruling. After a merge with migrations the supervisor re-runs the hosted DB setup.
6. **177** (#359): intake ACCEPT, at intake; after 175 merges, `route` and `job advance ... spec`.

Next free lead decision id: **B-4031**. Given out: 172 B-3998..B-4002, 178 B-4025..B-4029 (B-4025 used).

## Open jobs (stage)
172 approve r3, 173 needs-human, 174 approve r2 (APPROVE written), 175 needs-human, 176 needs-human, 177 intake (ACCEPT), 178 approve r1.

## Dependencies (job queue does not know them)
#273 needs #272; #274/#275 after #273; #284 needs #283; #290, #292, #294, #352 need #286; #358 needs #272; #360 needs #288; #363 needs #361; #364 needs #351 (new migrations); #295, #296 last. Hold those `new` lines until their dependency merges. When SP6, SP7 and SP8 finish, run each done-when demo and record it in STATUS.md.

## Traps learned
- **Wait for the builder's completion notification before `factory/bin/gate`:** the builder's SubagentStop hook (`gate --hook`) runs checks.sh in the same worktree for ~10 minutes after its READY message; a gate started meanwhile runs twice in one worktree. The hook can also make the agent end as "stalled 600s"; when build.md says READY and the tree is clean and pushed, gate anyway.
- TaskStop on a gate leaves its child processes: kill that tree with `taskkill /F /T /PID <env.exe pid of your own command>` only.
- `find .` from the repo root times out (huge `.worktrees`).
- Intake's Grep/Glob can time out; an agent can report a file it did not write: the scripts refuse, check with `ls`.
- A full `gates.py` run (170-merge) took 33 minutes with another factory gate alongside, no load flakes.
- A factory gate failed with `'eslint' is not recognized` in a job worktree while other gates ran: run `pnpm install --frozen-lockfile` in that worktree's `frontend` and rerun `factory/bin/gate` (run factory gates one at a time when possible).
- Load flakes (`[Errno 10048]`, WinError 10055): a targeted rerun passes (B-3984).
- Full gate runs catch stale pinned expectations that `checks.sh` skips (`tests/authz/test_catalog.py`, `tests/app/test_routes.py`, `tests/identity/test_schema.py` heads): tell builders of new modules or migrations to run them.
- `uv run alembic heads` does not work from `backend/` (no alembic.ini); the migrations have per-module heads by design.
- Never two full gate runs at once; tell merge agents to run `gates.py` once with a 2-hour background limit and to wait until no gates.py runs (PowerShell `Get-CimInstance Win32_Process`).
- Earlier traps hold: PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, full job ids only, `factory/bin/job` run directly (never `bash factory/bin/job`), worktree scripts via `uv run --frozen --project backend python ...`, DECISIONS id ranges per builder, isolation-reviewer is read-only (the lead saves its review), approver escalates any new migration by design, `job merge` can be rerun after a transient GitHub error.
- A lead must run a long gate as its own background Bash task: when an agent that started a gate in the background returns and the lead ends its turn, the `-p` process can exit and kill the run.
- The startup reads (whole STATUS.md, transcripts) cost 80k tokens: read STATUS.md progress lines with `cut`, and use `python scripts/context_size.py` early.
- Run one load generator or full suite at a time across builders: 178's CPU-load runs overlapped 172's `checks.sh`, whose frontend tests then failed on 5 s timeouts (36, then 307).
- A wait loop that greps process command lines can match its own; exclude its own PID.
