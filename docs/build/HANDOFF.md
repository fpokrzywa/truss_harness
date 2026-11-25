# Handoff — 2026-11-25 ~03:15 EST, charter 7 factory run (lead session agt-trus-harness-e5), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running; no agent, gate or wait loop running at handoff. Merged this session: batch 5 (PR #388, `cac3711`): 173, 184, 186, #364, S-054, S-055. Hosted DB setup done by the supervisor at `d35d815` (97 tables, 96 forced RLS). SP6 done-when demo passed on main and is recorded in STATUS.md.

## Rulings
- Supervisor: **seth-supervisor-3** (journaled 01:47:11). Find it with ListAgents; check `.devdata/supervisor/logs/supervising-session.log` before trusting a takeover. It writes S-* rows on docs-only PR branches `supervisor/rulings-*`: check that they change only DECISIONS.md, then take them in the next batch. Next free ruling: S-056. Never edit S-* rows.
- 192 needs no ruling (supervisor, 02:34): merge it with CI backend green on the exact head that merges.
- Escalations are ruled on by the supervisor. A conflict outside DECISIONS.md goes back to the supervisor before merging. Merge only with full gates and green CI on the head that merges (`--match-head-commit`, merge lock).
- After any merge with a migration: send "hosted DB setup due" with no full gate active, and start no full gate until the supervisor answers.
- A second `test_orphaned_holds` failure away from 00:00 UTC: stop and tell the supervisor (#366).
- If `scripts/supervisor/autostart.ps1` shows as deleted, neither commit the deletion nor restore it; tell the supervisor.

## Exact next step
1. `factory/bin/job queue` (background), then:
   - **189** (#374, PR #392, `80aebab4`, isolation-critical: authz, tenancy): gate PASS, at `review` round 1, no reviewer started. Spawn reviewer-code (opus), reviewer-security (opus) and isolation-reviewer. One existing assertion changes (stricter; `tests/mcp_access/test_project_key.py`): point the isolation reviewer and approver at it.
   - **190** (#375, PR #391, `394501c`): gate PASS, at `review` round 1, no reviewer started. reviewer-code and reviewer-security (opus), isolation-reviewer (read-only SQL checks). `:identifier(%(a)s)` is refused at publish although the run-time check accepts it (B-4097).
   - **191** (#384, PR #393, `0478201`): gate PASS, at `review` round 1, no reviewer started. Reviewers code and security (opus). Lead decision: allowlist only SQLSTATE 22012 with the exact constant text, existing assertions byte-identical (B-4098, B-4103).
   - **192** (#385, PR #389, `1498100`): approver APPROVE, at `approve`. Take it into batch 6.
2. Start **#290, #292, #294, #352** (unblocked by 173) and **#390** (after #385 merges); #295, #296 last. Slots: concurrency 8.
3. **Batch 6** (`lead/batch-6` from origin/main): 192 and whichever of 189, 190, 191 are approved (and any `supervisor/rulings-*`); expect DECISIONS.md conflicts only. One full gate and CI on its head.

Next free lead decision id: **B-4104** (B-4092..4094 job 189, 4095..4097 job 190, 4098..4100 job 191, 4101..4102 job 192, 4103 lead; older unused: B-4038, B-4039, B-4061, B-4062, B-4064, B-4066, B-4075, B-4077 to B-4080, B-4087, B-4088).

## Helper scripts (gitignored, `.worktrees/_gate-logs/`)
- `union_merge.sh <worktree> <remote-branch> <message>`: merge with DECISIONS.md by union; stops on any other conflict.
- `saverev.py <agent id> <out.md> <title>`: saves a reviewer's SubagentHandback as a review file.

## Dependencies (job queue does not know them)
#295, #296 last. #294 also takes 173 review 3 finding 2 (render must dispatch by kind with a refusing default, commented on #294), the `DockerBindings.workspace_host` claim check; #292 takes nested Palantir suffixes and `frontend_origin` before any fetch; #295 takes the five kinds in `TargetsView.tsx:52-61`. `select .e5from secrets` stays open for the live test: no live Snowflake app before it is answered (S-043, S-049). When SP8 finishes, run its done-when demo and record it in STATUS.md.

## Leftovers
- Branches `lead/batch-4`, `lead/batch-5` and worktrees `.worktrees/batch-3`, `batch-4`, `batch-4-merge`, `batch-5`, `batch-5-merge` are merged and may be removed. Scratch databases `shifde_184_iso3`, `shifde_364_iso1` were left by reviewers.
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

- A builder that is READY keeps its stop hook running: a factory gate started before the completion notification failed on a mypy INTERNAL ERROR (192); rerun PASS. Gate only after the notification.
- `gates.py` on 92e907a: pytest 1691 s with 3 builders alongside; 18/18.
