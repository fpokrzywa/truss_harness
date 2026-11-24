# Handoff — 2026-11-24 ~07:30 EST, charter 7 factory run (lead session 09217743), stopped early: owner reboot

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** the supervisor asked for an early handoff because the owner is rebooting; `.devdata/supervisor/PAUSE` is set by the supervisor. After the reboot, start the local cluster (`python scripts/dev_db.py start`) before anything else. No agent or gate is running at handoff.

## Finished this session
- Job 170 (#280 SP7-4, PR #350): round 2 fixed the code review's malformed-UUID finding (commit 4fb8c25); round 2 gate PASS; code PASS, security PASS. Advanced to stage `approve`; **approver not yet spawned**.
- Job 171 (#347, PR #349): merged: synced with main (DECISIONS rows by id; test_schema keeps runtime_0009, packaging_0004 heads), `gates.py --name 171-merge` 18/18 on `d718ffc` (full suite 6738 passed), PR merged as `2d691f8`; issue closed.
- Jobs 172 (#272 SP6-5) and 173 (#286 SP8-1): specs written (172: decision ids B-3998..B-4002; 173: B-4003..B-4007, migrations `delivery_0008_sp8_kinds` and `packaging_0005_sp8_target_kinds` allowed). Builders started round 1 and were **stopped for the reboot** with uncommitted work in their worktrees (172: 18 changed files, 173: 22; nothing committed or pushed).
- **Next free lead decision id: B-4008.** (B-3997 reserved for job 170's renumber.)

## Exact next step
1. Start the cluster; `PYTHONUTF8=1 factory/bin/job queue`.
2. **Job 170 (approve, restart):** spawn `approver` with the header and `PR: #350. Final round: 2.` On APPROVE run `factory/bin/job merge 170-sp7-4-assist-internal`. **Its DECISIONS row is numbered B-3987 (collides with #174's): renumber it to B-3997 when syncing (fix the DECISIONS conflict by hand if `job merge` stops on it).**
3. **Jobs 172 and 173 (build round 1, restart):** spawn a new `builder` named `builder-<job id>`, telling it it is resuming round 1: the previous builder was stopped mid-work and left uncommitted changes in the worktree (review them with `git status`/`git diff`, keep what fits the spec, finish, commit, push, open the draft PR, write build.md). Ports: 172 → 6820, 173 → 6830. Give the decision-id ranges above.
4. Dependencies (job queue does not know them): #273 needs #272; #274/#275 after #273; #283 needs #280; #284 needs #280 and #283; #288 needs #280; #290, #292, #294 need #286; #295, #296 last. Hold those `new` lines until their dependency merges. #147 stays open: its remaining parts (API key creator relation, delivery_approvals trigger) need a protected migration; file a factory issue for them or do it as lead hand work like #174.
5. When SP6, SP7 and SP8 finish, run each sub-project's done-when demo and record it in STATUS.md.

## Open jobs (stage)
170 approve (restart: approver), 171 merged, 172 build r1 (restart), 173 build r1 (restart).

## Traps learned
- A factory gate failed with `'eslint' is not recognized` in a job worktree while other gates ran: run `pnpm install --frozen-lockfile` in that worktree's `frontend` and rerun `factory/bin/gate` (run factory gates one at a time when possible).
- `tests/core/test_startup_role_check.py::test_api_serves_under_application_role` can fail with `[Errno 10048]` (port race) under load; a targeted rerun passes (B-3984).
- Full gate runs catch stale pinned expectations that `checks.sh` skips (`tests/authz/test_catalog.py`, `tests/app/test_routes.py` when a module adds permissions or routes): tell builders of new modules to run both.
- `uv run alembic heads` does not work from `backend/` (no alembic.ini); the migrations have per-module heads by design.
- For needs-human merges, give the merge agent a "wait until no gates.py runs" step; it lets two merges be queued safely.
- Earlier traps hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, full job ids only, never two full gate runs at once, `gates.py` output buffered until exit, reviewers sometimes forget to write their file).
- `wmic` is not available in bash here; to see whether a `gates.py` is running use PowerShell `Get-CimInstance Win32_Process | Where-Object CommandLine -match 'gates.py'`. Merge agents started duplicate gate runs because of that and short background time limits: tell them to run `gates.py` once with a 2-hour limit.
- `factory/bin/job merge` can fail on a transient `error connecting to api.github.com`; rerunning it works.
- Reviewers (ui especially) sometimes reply without writing their file: SendMessage them to write it.
- Decision-id collisions: builders take "next free id" from DECISIONS.md on main, so parallel jobs collide. Give each builder its id range in the prompt.
- The factory gate counts only modified/deleted protected files; a new migration passes the gate and the approver escalates it (by design, not a bug).
- `python` on PATH can resolve to the main checkout's venv: run demo scripts in a worktree with `uv run --frozen --project backend python scripts/<demo>.py`.
- isolation-reviewer is read-only: the lead writes `docs/build/reviews/<job>-isolation-review-1.md` from its reply and commits it on the branch.
- Before a reboot handoff, stopped builders leave uncommitted work in their worktrees; the next builder must be told to review it, not start clean.
