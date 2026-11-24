# Handoff — 2026-11-24 ~02:45 EST, charter 7 factory run (lead session dbf449f7)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** main = origin/main after this commit (code head b813e71, PR #337). Cluster on 55450 running. Lead context ~180k at handoff. No agent running, no gate run.

## Finished this session
- Merged 159 (#333, PR #334), 160 (#324, PR #335), 161 (#327, PR #336), 153 (#258, PR #323), 143 (#285, PR #308), 155 (#278, PR #328), 162 (#259, PR #337). Gate counts in the STATUS entry; ruling B-3984.
- Filed #339 (bound `SYSTEM$` names via `IDENTIFIER(?)`, depends on #312). Advisory posted on #261 (app preview must respect pinned prompt/tool visibility).
- **Next free lead decision id: B-3985.** (B-3981 used by 161, B-3983 on 163's branch, B-3982 unused.)
- PR #337 (162) and PR #328 (155) were each gated on main before the other merged; they touch different modules and GitHub merged cleanly, but the first full gate run of the next merge is the first full run over both.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`; start every `resume`/`new` line whose issue's "Depends on" is merged (`job queue` does not know dependencies). Now startable: **#261** (SP5-8; #259 and #260 merged), **#279** (SP7-3; #278 merged). Not yet: #262 (needs #261), #271-#275 (need #262), #280+ (need #279), #339 (needs #312), SP8 chain.
2. **163** (#312, PR #338) needs-human: approver escalated only for the adapter outbound rule; code/security PASS. As human: accept the behaviour change (it only refuses more), spawn `isolation-reviewer` if wanted (read-only SQL check, not org data), then an agent to merge origin/main in `.worktrees/163-read-only-sql-check`, `python scripts/gates.py --ref <sha> --name 163-merge`, `gh pr merge 338 --merge --match-head-commit <sha>`. Then #339 is startable.

## Open jobs (stage)
163 needs-human. No other job in a running stage.

## Traps learned
- A factory gate failed with `'eslint' is not recognized` in a job worktree while other gates ran: run `pnpm install --frozen-lockfile` in that worktree's `frontend` and rerun `factory/bin/gate` (run factory gates one at a time when possible).
- `tests/core/test_startup_role_check.py::test_api_serves_under_application_role` can fail with `[Errno 10048]` (port race) under load; a targeted rerun passes (B-3984).
- Full gate runs catch stale pinned expectations that `checks.sh` skips (`tests/authz/test_catalog.py`, `tests/app/test_routes.py` when a module adds permissions or routes): tell builders of new modules to run both.
- `uv run alembic heads` does not work from `backend/` (no alembic.ini); the migrations have per-module heads by design.
- For needs-human merges, give the merge agent a "wait until no gates.py runs" step; it lets two merges be queued safely.
- Earlier traps hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, full job ids only, never two full gate runs at once, `gates.py` output buffered until exit, reviewers sometimes forget to write their file).
