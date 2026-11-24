# Handoff — 2026-11-24 15:15 EST, charter 7 factory run (lead session b10341d0), written early as a safety record

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file. The previous lead (847b16bd) exited at 15:05 without a handoff record; its gate run of 173 died with it (no result).

## Rulings (supervising session)
- 173: S-040, conditional: merge `155d4c30` only after `gates.py --name 173-merge` passes **every gate in full** on that commit. If a worker C1 or job-kind test fails there, do not merge as "intermittent": keep the FAILED lines for a ruling.
- 175 (PR #357, `a86fa42a`) and 176 (PR #356, `489bf9af`): **NOT ruled, hold both** (S-041 and the next number, queued in `new_session_handoff.md` for the next supervising session). 176: the red CI (C1 [cancel]/[deadline], `test_jobkinds`) counts against approval until #361 finds the cause or shows that the diff cannot reach it.
- Next supervisor ruling S-041 (theirs). **Next free lead decision id: B-4030** (178: B-4025..B-4029; 172: B-3998..B-4002).

## Open jobs
- 172 (#272 SP6-5, PR #353): build round 3 on `feedback-1.md` (B-4009), builder started 15:11. Round 2: isolation review 2 APPROVED, CI red on a guard test (the Snowpark dev pin). Round 3 changes need gate, code and security reviews and an isolation look at the new diff.
- 173 (#286 SP8-1, PR #354): needs-human, gates running since 15:09 on `155d4c30` (`/tmp/gates173.out`, logs `.worktrees/_gate-logs/173-merge/`).
- 174 (#283 SP7-7, PR #355, `0d2e6c36`): round 2 gate, code, ux, ui PASS; at approve (approver started 15:11); CI backend red on C1 only (#361).
- 175 (#288), 176 (#351): needs-human, held (above).
- 177 (#359): intake ACCEPT, at intake; hold until 175 merges (its spec needs a test file of 175's branch), then route + `job advance ... spec`.
- 178 (#361): the intermittent approved-write tests; at intake. The cause is not known.

## Dependencies (job queue does not know them)
#273 needs #272; #274/#275 after #273; #284 needs #283; #290, #292, #294, #352 need #286; #358 needs #272; #360 needs #288; #295, #296 last. Hold those `new` lines until their dependency merges. When SP6, SP7 and SP8 finish, run each done-when demo and record it in STATUS.md. After 173 merges, the supervising session re-runs the hosted DB setup.

## Exact next step
`PYTHONUTF8=1 factory/bin/job queue`, then the jobs above from their stage.

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
