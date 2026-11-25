# Handoff — 2026-11-25, charter 7 factory run (lead session agt-trus-harness-4a), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running. Merged this session: batch 8 (PR #422, `b201c9e`, gates 18/18 on `898d5315`, 8550 passed, 13 skipped, CI green on that head, S-065): #294 (job 195, SP8-9 Docker), #401 (job 199), S-063 to S-065, B-4159. No migration, no hosted setup due. Main CI for `b201c9e` was not read at handoff. No agent, gate or wait loop running at handoff.

## Rulings
- Supervisor: **seth-supervisor-9** (took over 08:19; check `.devdata/supervisor/logs/supervising-session.log`). S-063 to S-065 are in batch 8. Next free ruling S-066. Message the supervisor first. Never edit S-* rows.
- S-059/S-060: content of 194 (3c4def8, B-4123) and 193 (c541744, B-4107, table A) accepted; NOT merge approvals. S-061: round 2 needs the delta reviewed only; one shared re-check helper (195's `_recheck`/`authorize`).
- **Order after 195: 194, 193, one at a time.** Each: merge main, resolve, code/security/isolation review of the delta and of every resolved conflict, the three demos, own CI, full gates, then a supervisor ruling. Each reuses 195's helper. 196 added two key binds (`requested_api_key_id`, `decided_api_key_id`) to `repository.insert_plan`/`insert_approval`: 193/194 must pass them.
- S-064: job 200's seven changed test values accepted as content; its code reviewer confirms from the test diff that nothing else changed and that the differential's data file is recorded from 7cd5284's source.
- S-065: #421 is done before any live Docker validation (SP8-11, #296). Any conflict other than DECISIONS.md goes to the supervisor as a diff.
- 199: its two earlier stalls stay "cause unknown" (never closed as fixed or flaky load); a further stall of tests/runtime/test_worker.py on any head goes to the supervisor. A second failure of #401's, #366's or #416's test on any head goes to the supervisor before any merge.
- After a merge with a migration: "hosted DB setup due" to the supervisor, no full gate active while it runs. Batch 8 has none.
- No rerun-and-merge on a red CI. Merge only with full gates and green CI on the head that merges (`--match-head-commit`, merge lock). autostart.ps1 shown as deleted: neither commit nor restore.
- Worktree removal: end the owning process first, then remove without `--force` (the lead removed 199-load under a running pytest).

## Exact next step
1. `factory/bin/job queue` (the lead's last run failed on the GitHub GraphQL rate limit, so 195 and 199 still need their move to merged; wait for the limit to reset). ListAgents, message the supervisor; read main CI on `b201c9e`.
2. Approvers/reviews for 198, 201, 202, 203 and the gate of 200, as listed under Jobs. Supervisor-9: batch 9 takes whichever of 198, 201, 202, 203 the approver APPROVEs (203 early, so later heads do not hit #416; any ESCALATE or change outside the normal path goes to the supervisor).
3. Feedback and new builder for 194 (PR #405) round 2, now on main `b201c9e`: main merged by its builder, explicit (platform, kind) pairs with a refusing else, the shared #406 mechanism, #407, the two key binds, 195's `Runner._recheck`/`authorize` before every runtime call; then delta and conflict-resolution and isolation reviews, demos, CI, own ruling. 193 after 194, same pattern. #421 before SP8-11.
4. Batch 9 = `lead/batch-9` from origin/main.

## Jobs
- **198** (#394, PR #411, `d33c5fa`): gate, code, security and isolation review 1 PASS/APPROVED (round-1/isolation-review-1.md; two LOW test gaps, findings 1 and 2, both could be added by the builder; needs one `DECISIONS.md` union with main). At review: `job advance 198-remaining-slug-oracles-project approve`, then the approver (put the isolation review in its brief).
- **200** (#398, PR #423, `8039524`, READY, at `gate`; the builder agent was reported stalled after its hand-back: worktree clean and in sync with origin, diff limited to the planned files): run the gate, then reviewer-code, reviewer-security (opus), isolation for the SQL checks, approver. S-064 applies.
- **201** (#410, PR #417, `bcf243f`): gate, code, security PASS; at `approve`. Start the isolation-reviewer (gateway, stopped archived-target window) and then the approver.
- **202** (#415, PR #418, `961539c`): gate, code PASS; at `approve`. Isolation-reviewer (worker advisory lock across clients), then the approver. PR body says `Closes #415`; item 1 is #419.
- **203** (#416, PR #420, `dfb4b13`, gate PASS, at `review`): reviewer-code (opus), then approver. Test helper only; its build.md has a product-side note for the supervisor (a worker's `run()` can return while a beat is in progress; not built).
- 193 (#290, PR #404), 194 (#292, PR #405): needs-human, wait for 195's merge. Then `factory/bin/job feedback 292` with `factory/jobs/194-*/lead-feedback-draft.md`, and 193's for #290 after 194; new builders.
- New issues: #421 (Docker window and unpinned grant checks, factory, after 193/194), #397 (after 194), #406/#407 (folded into 193/194 round 2 per the drafts, do not start separately), #419 (no factory label: needs a migration, #415 item 1), #395 (owner's, no label), #295, #296 last. Concurrency 8.
- CI on main 7cd5284 was red once (run 45611337978, test_r1_round5 test, see #416/job 203); its rerun (information only) result unread at handoff unless stated above.

Next free lead decision id: **B-4163** (4150-4152 job 200, 4153-4155 job 201 (4153 used), 4156-4158 job 202, 4159 lead, 4160-4162 job 203 (4160 used)).

## Leftovers of this session
- Worktrees `195-iso`, `198-iso`, `199-base`, `199-head` (databases shifde_195_iso, shifde_198_iso, shifde_199_base, shifde_199_head, shifde_199_load) and the half-removed folder `.worktrees/199-load` may be removed by hand. `.worktrees/batch-8` after the merge. Two approver temp items remain in the user's temp folder (harmless).
- A shell `cd` into `.worktrees/_gate-logs/...` or a batch worktree moves the session's directory: use absolute paths and `git -C`.
- A background command's child loops die when the command ends: run loops inside the background command itself.

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
