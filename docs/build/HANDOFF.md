# Handoff — 2026-11-24 23:15 EST, charter 7 factory run (lead session 8dc8e81b), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running; no agent, gate or wait loop running at handoff. Merged this session: 182, 183 and 180 as batch PR #376 (`0b7d7bb`, 18/18 and CI green on `6644fd6`). SP7's done-when demo passed on `0b7d7bb` (STATUS).

## Rulings
- Supervisor cee9405c (`agt-trus-harness-e6`; confirmed by the former one) ruled S-044..S-048 (DECISIONS) and then stopped at its own context limit. Its rulings stand. Its successor will introduce itself: hold for it 173's ruling (S-049 is its next free id), any conflict outside DECISIONS.md, 181 and the hosted database step.
- **176** (#351, PR #356, accepted `776f63b`, S-048, migration assist_0002): sync onto main, full gates and CI on the synced head, then merge; then "hosted DB setup due" to the supervisor and no full gate until it answers. It edits `assist/service.py` and `test_sp7_tools.py` like 182: expect a conflict outside DECISIONS.md, which goes to the supervisor with the resolution before merging.
- **177** (#359, PR #372, accepted `25e0d9c`, S-046): conflicts with 182 (now on main) in `backend/tests/mcp_access/test_sp7_tools.py`: each side adds its own test at the same place (182's `test_a_context_of_p2_is_not_found_in_p1_though_the_key_reads_both`, 177's `test_a_p1_key_gets_not_found_for_another_workspace_and_a_workspace_level_target`; the hunks interleave because their signatures match). Proposed resolution: keep both tests whole. Needs the supervisor's agreement before merging.
- **173** (#286, PR #354, `f83be49`, needs-human): gate, code, security PASS; isolation review 3 APPROVED on that head with a hazard now real (180 is on main): the merge is text-clean in `delivery/plans.py` but gives a wrong-platform kind gate, and conflicts in render.py, runner.py, executor/support.py, test_routes_cross_client.py. decision.md lists the round 4 changes (its own deploy map; the branch's own 12 + 3 cases change their expected code to `target_kind_mismatch`, which needs a ruling first; a 5x5 gate test; a new decision id). Ask for that ruling, then `job feedback 286`.
- Escalations are ruled on by the supervisor, not the lead. The lead may send a job back with `job feedback`.
- After 173 and after 176 merge: "hosted DB setup due". Tell the supervisor; go on with the next gate only as it says.
- Merge only with green CI on the head that merges: sync and push first, wait for CI, then `job merge` (or the hand merge with `--match-head-commit`).
- A second `test_orphaned_holds` failure away from 00:00 UTC: stop and tell the supervisor (#366).
- Never skip, loosen or add retries to a test. Isolation review APPROVED does not settle that.
- If `scripts/supervisor/autostart.ps1` shows as deleted, neither commit the deletion nor restore it; tell the supervisor.


## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`, then:
   - **184** (#366, PR #367, `8074343`) at approve: isolation review 2 BLOCKED (`docs/build/reviews/184-isolation-review-2.md`; mutations re-run by the reviewer: build.md's claim is true, but `daily_since` takes its window from `budget_reservations.created_at`, so 5 tests accept a hold dated 3 days back that main refuses). Approver with that file, then needs-human, then `job feedback 366`: the reviewer's tests-only fix (window from `budgets.created_at`) and finding 2.
   - **181** (#358, PR #369, `101c0e4`) round 3 gate PASS, at review: spawn code and security (opus) and isolation review 3 (the `1.e5from` family, `_ends_from`; builder fuzz 600,000 texts with nothing lost against main), then the approver. CI `backend` was red on `2e47825` from #377's chance marker, not from the diff: green CI needed on the head that merges.
2. Sync 176, and 177 after a ruling; gate them (one run may hold both, S-044). 173 as above.
3. New, unblocked by 273's merge: #274, #275 (its two lead comments), and #373 (360 merged). #377 (flaky cross-client export marker, tests only, isolation review) is free too. `job queue` offers #290, #292, #294, #295 as `new`: not yet (see Dependencies).

Next free lead decision id: **B-4073** (B-4038, B-4039, B-4061, B-4062, B-4064, B-4066 also free). Given out: 184 B-4065..4066, 181 B-4067..4069, 173 B-4070..4072.
Isolation reviews saved this session: `173-isolation-review-3.md`, `181-isolation-review-2.md` (BLOCKED), `184-isolation-review-2.md` (BLOCKED). Filed: #377; noted on #375.

## Dependencies (job queue does not know them)
#290, #292, #294, #352 need #286; #294 also takes 173 review 3 finding 2 (render must dispatch by kind with a refusing default) and note 3; #364 needs #351 (new migrations); #374 needs #359; #375 needs #358; #295, #296 last. #358 before any live Snowflake app. When SP6 and SP8 finish, run each done-when demo and record it in STATUS.md. `job queue` lists blocked issues as `new`: start only those whose dependency is merged.

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
- Read an agent's file before advancing: one spec-writer reported done without having written `spec.md`. Run `job advance` first and spawn the next agent only after it succeeds, not in the same batch.
- `job advance <id> build` refuses when every routed review passed, even if an isolation review found something. The route is approve, approver ESCALATE, needs-human, `job feedback`; point the approver at the review file.
- The "Wait for builder's completion notification before a gate" trap cost one overlap (183); a builder can also stall after READY (177): check the worktree is clean and equal to origin, then gate.
- A factory gate ran 4 to 17 minutes with a full gate and reviewers alongside. `pytest` logs in `_gate-logs` appear only when the suite ends (24 minutes).
- Isolation reviews are saved from the agent's `SubagentHandback` call in `~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl`.
- A builder can hand back its report and then be reported `failed: stalled`, with its own background shell still running (176, 184): check the worktree is clean, equal to origin, and that a mutation it described is reverted before the gate.
- A reviewer may return its review as a message and not write `review-<r>.md`: resume it (SendMessage by its id) to write the file.
- `scan-secrets.py` lists about 70 test-fixture lines: pipe it to `tail -1`.
- Agents' review saves: read the `SubagentHandback` input in the agent's `.jsonl` with a small script (one exists at `/tmp/saverev.py`, may be gone; arguments: agent id, output path, title).
- A shell `cd` into a worktree made the session's working directory that worktree for good: run the main checkout's scripts by absolute path (`/s/.../factory/bin/job` works from anywhere) and use `git -C`. `.worktrees/batch-3` (branch `lead/batch-3`, merged) is left over.
- Batch merge that worked: branch `lead/batch-<n>` from origin/main, `git merge --no-ff` each job branch, union merge of DECISIONS.md only (`git merge-file --union`), one PR with `Closes #n` lines, `gates.py` and CI on its head, `gh pr merge --merge --match-head-commit`: GitHub then marks the job PRs merged and `job queue` moves the jobs.
- `S:/tmp/HANDOFF.draft.md` is an older session's stray draft: leave the folder alone.
