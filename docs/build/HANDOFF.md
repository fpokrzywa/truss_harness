# Handoff — 2026-11-25 ~01:20 EST, charter 7 factory run (lead session 51a7f901), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running; no agent, gate or wait loop running at handoff. Merged this session: batch 4 (PR #383, `9e68ec2`): 177, 181, 185, 187, 188 and rulings S-049 to S-053.

## Rulings
- Supervisor: **seth-supervisor-2** (session 1b5af199, journaled 00:40:47). Find it with ListAgents; check `.devdata/supervisor/logs/supervising-session.log` before trusting a takeover. It writes S-* rows on docs-only PR branches `supervisor/rulings-*`: check that they change only DECISIONS.md, then take them in the next batch. Next free ruling after S-054: S-055. Never edit S-* rows.
- **Batch 5**: after it merges, send "hosted DB setup due" (runtime_0010, assist_0003) and start no full gate until the supervisor answers. Any further migration edit comes back (S-054).
- **173** needs a ruling after its reviews (S-051 is no merge approval): send `bbbe14d` with both reviews, the isolation review and the approver's decision. The docstring conflict in `tests/identity/test_schema.py` is accepted only if the assertions are both sides' whole; #364 (batch 5) moves pinned heads in the same file, so expect a further conflict when 173 syncs after it.
- Escalations are ruled on by the supervisor. A conflict outside DECISIONS.md goes back to the supervisor before merging. Merge only with full gates and green CI on the head that merges (`--match-head-commit`, merge lock).
- A second `test_orphaned_holds` failure away from 00:00 UTC: stop and tell the supervisor (#366).
- If `scripts/supervisor/autostart.ps1` shows as deleted, neither commit the deletion nor restore it; tell the supervisor.

## Exact next step
1. **Batch 5 (PR #388, branch lead/batch-5, worktree `.worktrees/batch-5`, `eabbd42d`)**: `python -u scripts/gates.py --ref eabbd42d... --name batch-5-merge` in the background (CI at 01:45 EST: backend=pending frontend=pass scripts=pass secret-scan=pass). 18/18 and CI green on that head: merge with the lock and `--match-head-commit`, `git merge --ff-only`, `job queue` (about 3 minutes, run it in the background: it removes merged jobs' worktrees). Close anything GitHub did not close (#275, #366, #364, PRs #381, #367, #386, #387). Tell the supervisor; wait for its hosted answer.
2. Then the **SP6 done-when demo on main** (`scripts/snowflake_demo.py --fake`, see CLAUDE.md after the merge; main checkout's own `backend/.venv`), recorded in STATUS.md.
3. **173** (#286, PR #354, `bbbe14d`): round 4 gate PASS, code PASS; security PASS, isolation review 4 APPROVED (`docs/build/reviews/173-isolation-review-4.md`); stage approve. Spawn the approver with that file (it escalates migrations by design), then `job advance ... needs-human` and send the ruling request. Its merge with main 9e68ec2 conflicts only in DECISIONS.md; with batch 5 expect `tests/identity/test_schema.py` too. Review 4 finding 1 (front end shows Fabric/Palantir/Docker targets as Databricks: `TargetsView.tsx:52-61`, `delivery-types.ts`) goes as a comment on #295.
4. Start intake for the issues unblocked now: **#374** (needed #359, merged), **#375** (needed #358, merged), **#384**, **#385** (need #373 and #377, merged). Jobs 184 and 186 still hold slots until batch 5 merges; `job queue` lists #290, #292, #294, #295, #296, #352 as startable: not before 173 merges (295, 296 last). #384 changes the same log code as #373.

Next free lead decision id: **B-4092** (B-4087, B-4088 unused by 173; B-4085, B-4086 used; B-4089, B-4090 #364; B-4091 lead; older unused: B-4038, B-4039, B-4061, B-4062, B-4064, B-4066, B-4075, B-4077 to B-4080).

## Helper scripts (gitignored, `.worktrees/_gate-logs/`)
- `union_merge.sh <worktree> <remote-branch> <message>`: merge with DECISIONS.md by union; stops on any other conflict.
- `saverev.py <agent id> <out.md> <title>`: saves a reviewer's SubagentHandback as a review file.

## Dependencies (job queue does not know them)
#290, #292, #294, #352 need #286; #294 also takes 173 review 3 finding 2 (render must dispatch by kind with a refusing default) and note 3; #295, #296 last. #358 is merged; `select .e5from secrets` is open on #375 and the live test: no live Snowflake app before it is answered (S-043, S-049). When SP6 and SP8 finish, run each done-when demo and record it in STATUS.md.

## Leftovers
- Branch `lead/batch-4` and worktrees `.worktrees/batch-3`, `batch-4`, `batch-4-merge` are merged and may be removed. Scratch databases `shifde_184_iso3`, `shifde_364_iso1` were left by reviewers.
- The Agent tool's `isolation: worktree` puts worktrees in `.claude/worktrees/`, which is not gitignored and makes main dirty: do not use it; create `.worktrees/<name>` by hand instead.
- `gates.py ... | tail` hides its exit code: read its lines.

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
