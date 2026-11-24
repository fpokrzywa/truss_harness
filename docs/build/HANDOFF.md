# Handoff — 2026-11-24 ~04:55 EST, charter 7 factory run (lead session 98413370)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** main = origin/main after this commit (code head 125a6c1, PR #344). Cluster on 55450 running. Lead context ~190k at handoff. No agent running, no gate run.

## Finished this session
- Merged 163 (#312, PR #338), 164 (#261, PR #341), 165 (#279, PR #342), 167 (#340, PR #343), 166 (#262, PR #345), 168 (#339, PR #344). Sub-project 5 done-when recorded: met against the fake platform, live validation waiting on owner input.
- Filed #340 (app-preview route, missing from the SP5 cut; B-3985). Posted SP7-3 review follow-ups on #280 (resume pre-checks, job_id check, assert allow-list, idempotency race, proposal payload exposure).
- **Next free lead decision id: B-3987.**

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`. Jobs **169-sp6-4-packaging-target** (#271) and **170-sp7-4-assist-internal** (#280) exist at stage intake, no agent run yet: spawn `intake` for each. Their dependencies are merged.
2. Then start in dependency order (check each issue's "Depends on" first; `job queue` does not know dependencies): #272 needs #271; #273 needs #272; #274/#275 follow; #283 needs #280; #284 needs #280 and #283; #286 needs #271; #288 needs #280; #290, #292, #294 need #286; #295 and #296 last.
3. When SP6, SP7 and SP8 finish, run each sub-project's done-when demo and record it in STATUS.md.

## Open jobs (stage)
169 intake (not started), 170 intake (not started). Nothing else running.

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
