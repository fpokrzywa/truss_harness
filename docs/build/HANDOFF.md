# Handoff — 2026-11-24 ~13:50 EST, charter 7 factory run (lead session 93ea764f), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running; no agent or gate running at handoff. `PAUSE` is cleared.

## Finished this session
- Job 170 (#280 SP7-4, PR #350) merged `889377a` (B-4008, S-039).
- Filed #351 (assist delete guard, dismiss-race audit, review advisories; job 176) and #352 (rest of #147: API key creators, approvals trigger; depends on #286 because it adds `delivery_0009`). #147 closed.
- **Next free lead decision id: B-4009, then B-4025.** Given out: 176 B-4010..B-4014, 174 B-4015..B-4019, 175 B-4020..B-4024 (172: B-3998..B-4002; 173: B-4003..B-4007). Next supervisor ruling is S-040 (theirs).

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`.
2. **172 (build round 2, start):** new builder `builder-172-sp6-5-appgen-render`, port 6820: address `round-1/` reviews (code CHANGES, security CHANGES) AND `docs/build/reviews/172-isolation-review-1.md` (BLOCKED; fix findings 1-3), push, update build.md. Then gate, reviewers, and isolation review 2.
3. **173 (approve, round 1):** gate, code and security PASS, but isolation review 1 BLOCKED (`docs/build/reviews/173-isolation-review-1.md`, finding 1). Spawn `approver` with the header, `PR: #354. Final round: 1.` and the path of that review (it must ESCALATE); `job advance ... needs-human`; `job feedback` with finding 1 and its tests and note 3; new builder (port 6830) for round 2; then gate, reviewers, isolation review 2, sync with main, full gate, hand merge (two new migrations, so it is human-gated).
4. **174 (#283, port 6840) and 175 (#288, port 6850): build round 1, start** builders with their decision-id ranges. 175's spec changes one existing 422 case in `tests/mcp_access/test_settings.py` (spec-listed) and carries the spend cap in the `assist.route` payload.
5. **176 (#351): build round 1, start** a builder (port 6860, ids B-4010..B-4014). Its cross-client test change keeps every assertion (only the cutoff and the `_seed` row ages change, spec 9c, accepted by the lead).
6. Dependencies (job queue does not know them): #273 needs #272; #274/#275 after #273; #284 needs #283; #290, #292, #294, #352 need #286; #295, #296 last. Hold those `new` lines until their dependency merges.
7. When SP6, SP7 and SP8 finish, run each sub-project's done-when demo and record it in STATUS.md.

## Open jobs (stage)
172 build r2 (start), 173 approve r1 (isolation BLOCKED), 174 build r1 (start), 175 build r1 (start), 176 build r1 (start).

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
