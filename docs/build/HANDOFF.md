# Handoff — 2026-11-25, charter 7 factory run (lead session agt-trus-harness-b9), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running. Merged this session: batch 7 (PR #412, `254dcbc`, main CI green, gates 18/18, 8395 passed; hosted DB setup done): 191, 196 (migration `delivery_0009_related_approvers`), 197, S-059 to S-062. No agent, gate or wait loop running at handoff.

## Rulings
- Supervisor: **seth-supervisor-7** (find it with ListAgents; check `.devdata/supervisor/logs/supervising-session.log`). S-059 to S-062 are on main. **S-063** (branch `supervisor/rulings-s063`: 199 test diff accepted, condition #415 filed) goes in the next batch. Next free ruling S-064. Message the supervisor first. Never edit S-* rows.
- S-059/S-060: content of 194 (3c4def8, B-4123) and 193 (c541744, B-4107, table A) accepted; NOT merge approvals. S-061: 195's round 1 expectations and B-4125..B-4129 accepted; round 2 needs the delta reviewed only; one shared re-check helper. S-062: 196 with delivery_0009 approved for batch 7 on full gates and green CI of the exact head. No lead-made source conflict resolution.
- **Order after batch 7: 195, 194, 193, one at a time.** Each: merge main, resolve, code/security/isolation review of the delta and of every resolved conflict, the three demos, own CI, full gates, then a supervisor ruling. 193 and 194 wait for 195's merge (the helper). 196 added two key binds (`requested_api_key_id`, `decided_api_key_id`) to `repository.insert_plan`/`insert_approval`: 193/194/195 must pass them where they call these.
- **#401 (job 199) diff is owed to the supervisor** (wait-only edit of an existing test: accepted only if every assertion and the 60 s timeout are unchanged and the reproduction is shown; its build.md answers whether a 4,500-client sweep is a product limit). Forward the exact diff.
- After a merge with a migration: "hosted DB setup due" to the supervisor, no full gate active while it runs.
- A second failure of #401's or #366's test on any head goes to the supervisor before any merge. No rerun-and-merge on a red CI: send the FAILED lines first. Merge only with full gates and green CI on the head that merges (`--match-head-commit`, merge lock). autostart.ps1 shown as deleted: neither commit nor restore.
- S-061 and S-063 ask: reviewers of 195 round 2 look at B-4145's residual window (a write can start under a grant up to 30 s old plus the checkpoint and CLI version checks) and mutate `_recheck` and at least one `authorize` call. The reviewer that blocked 195 lived in the old session: start a fresh isolation-reviewer with round-1/isolation-review-2.md.
- **199 needs a comparison series before it merges (S-063, amended on `supervisor/rulings-s063`).** 2 of 17 `-n 8` runs of the test file stalled (cause not established, overlapping batch 7's full suite). Put both before its reviewers and approver: can the diff cause them? Run the same file, same `-n 8`, the same count on `254dcbc7` and on `3d586dde`, each with comparable other load; no stall on the head that the base does not show; on a stall keep a thread dump (faulthandler dump_traceback_later) before killing it. A stall on both is its own issue and goes to the supervisor. Any product change, or a change of the 60 s or an assertion, comes back.

## Exact next step
1. `factory/bin/job queue` (about 2 minutes; background). ListAgents, message the supervisor.
2. **195** (#294, PR #403, `33580483`, synced on 254dcbc, gate PASS, review round 2): reviewer-code and reviewer-security (opus) on the delta `48b4d5b..33580483` excluding main's own changes, isolation-reviewer (fresh, see Rulings); then approver. Then the batch-8 gates and CI on the exact head, supervisor ruling, merge; it is the first of 195, 194, 193.
3. **198** (#394, PR #411, `d33c5fa`, gate PASS, review round 1): reviewer-code, reviewer-security (opus) and an isolation-reviewer (project and workspace visibility, RLS-sensitive: existing assertions unchanged, #394 body). Spec ids B-4134, B-4135 used.
4. **199** (#401, PR #413, `3d586dd`, builder READY, at stage `gate`, gate not run): `factory/bin/gate`, reviewer-code (opus), approver. Its existing-test edit is accepted by S-063 if nothing else changed.
5. After 195 is merged: `factory/bin/job feedback 292` with `factory/jobs/194-*/lead-feedback-draft.md` (and 193's for #290 after 194); start a new builder for each (the old builders' sessions are gone). 194's PR #405 is synced with main at `6e850e6`.
6. New issues to start when slots free: #397 (after 194), #398, #406/#407 (folded into 193/194 round 2 per the drafts, so do not start them separately), #410 (gateway checkpoint), #415 (client sweep), #395 (owner's, no label), #295, #296 last. Slots: concurrency 8.
7. Batch 8 = `lead/batch-8` from origin/main: S-063, then whichever of 195, 198, 199 are approved. DECISIONS.md by union only; any other conflict goes to the supervisor.

Next free lead decision id: **B-4150** (4131-4133 job 197 unused, 4136-4139 job 198 free, 4142-4143 job 199 free, 4140 4148 4149 lead, 4144-4145 job 195 used, 4146-4147 free; older unused ids as before).

## New this session (traps)
- `gates.py --name X` checks out the ref into `.worktrees/X` and detaches it: never use the name of your own batch worktree (it took over `.worktrees/batch-7`); use `<batch>-merge`.
- The factory gate takes about 5 minutes; the full gates 36 minutes with pytest 1699 s (3 builders alongside).
- `union_merge.sh` takes the branch name without `origin/`. `saverev.py <agent id> <out> <title>` saves the LAST long hand-back of an agent (a second hand-back overwrites the first file if you reuse the name).
- A reviewer asked a precise follow-up question reversed its verdict (195): ask reviewers about the two S-060 questions (token cache, stored rows) on every executor job.

## Helper scripts (gitignored, `.worktrees/_gate-logs/`)
- `union_merge.sh <worktree> <remote-branch> <message>`: merge with DECISIONS.md by union; stops on any other conflict.
- `saverev.py <agent id> <out.md> <title>`: saves a reviewer's SubagentHandback as a review file.

## Dependencies (job queue does not know them)
#295, #296 last. #294 also takes 173 review 3 finding 2 (render must dispatch by kind with a refusing default, commented on #294), the `DockerBindings.workspace_host` claim check; #292 takes nested Palantir suffixes and `frontend_origin` before any fetch; #295 takes the five kinds in `TargetsView.tsx:52-61`. `select .e5from secrets` stays open for the live test: no live Snowflake app before it is answered (S-043, S-049). When SP8 finishes, run its done-when demo and record it in STATUS.md.

## Leftovers
- `.worktrees/batch-6` (branch `lead/batch-6`, PR #399 closed, superseded) and the half-removed folders `.worktrees/batch-3-merge`, `batch-4-merge`, `batch-5-merge` may be removed by hand (long paths; git no longer lists them).
- `lead-feedback-draft.md` in 191's job folder was sent as feedback-1. `.worktrees/_gate-logs/uvplug.py` is a one-off reproduction plugin.
- `factory/bin/job queue` took more than 2 minutes. A builder of SP8 size took 100 minutes and 500k tokens: split further slices.
- Demos from a worktree: `uv run --frozen --project backend python scripts/<demo>.py --fake` (plain `python` is the main checkout's venv).
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
