# Handoff — 2026-11-24 ~06:40 EST, charter 7 factory run (lead session 5c5285b9)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** main = origin/main after this commit (code head 58f6615, PR #348). Cluster on 55450 running. Lead context ~180k at handoff. No agent or gate running.

## Finished this session
- Merged #174 lead hand work (PR #346, runtime_0008, B-3987) and #271 SP6-4 (job 169, PR #348, packaging_0004, B-3988..B-3992, B-3995). Both 18/18 full gates.
- Filed #347 (review note 6 of #174): job 171 built and reviewed, needs sync + full gate + merge (below).
- **Next free lead decision id: B-3998.** (B-3993/B-3994 job 171, B-3995 lead 169, B-3996 lead 171, B-3997 reserved for job 170's renumber.)

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`.
2. **Job 170 (#280 SP7-4, PR #350):** factory gate round 1 PASS; stage `review` round 1: spawn reviewer-code and reviewer-security (model opus) in one message. **Its DECISIONS row is numbered B-3987 (collides with #174's): at merge, renumber it to B-3997.** It touches no migration; on APPROVE run `factory/bin/job merge` (fix the DECISIONS conflict by hand if it stops on it).
3. **Job 171 (#347, PR #349, needs-human, head f70a213):** isolation APPROVED (record committed), lead edits done (B-3996). Remaining: in `.worktrees/171-retention-delete-guard-enforce` run `git fetch origin && git merge origin/main` (expect conflicts in `docs/build/DECISIONS.md` (keep both, order by id) and `backend/tests/identity/test_schema.py` docstring (combine runtime_0009 + packaging_0004 sentences; the asserts auto-merge: keep `runtime_0009 in heads`, `runtime_0008 not in heads`, `packaging_0004 in heads`)), push, then one `gates.py --ref <sha> --name 171-merge` via a verifier agent, then `gh pr merge 349 --merge --match-head-commit <sha>`.
4. Jobs **172 (#272 SP6-5)** and **173 (#286 SP8-1)** are at stage `spec` (intake ACCEPT, routed code+security): spawn `spec-writer` for each. 173 intake RESCOPED: read intake.md's first lines. Migrations on job branches are allowed (approver escalates, lead merges after isolation review, B-2530).
5. Dependencies (job queue does not know them): #273 needs #272; #274/#275 after #273; #283 needs #280; #284 needs #280 and #283; #288 needs #280; #290, #292, #294 need #286; #295, #296 last. #147 stays open: its remaining parts (API key creator relation, delivery_approvals trigger) need a protected migration; file a factory issue for them or do it as lead hand work like #174.
6. When SP6, SP7 and SP8 finish, run each sub-project's done-when demo and record it in STATUS.md.

## Open jobs (stage)
170 review (r1), 171 needs-human (merge pending), 172 spec, 173 spec.

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
