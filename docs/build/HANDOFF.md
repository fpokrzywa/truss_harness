# Handoff — 2026-11-25 ~05:10 EST, charter 7 factory run (lead session agt-trus-harness-80), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running. Merged this session: batch 6 (PR #400, `2184df2`, no migration, no hosted run due): 192, 189, 190, S-056..S-058. No agent, gate or wait loop running at handoff.

## Rulings
- Supervisor: **seth-supervisor-5** (took over 05:01; check `.devdata/supervisor/logs/supervising-session.log`; find it with ListAgents). Next free ruling S-059. It writes S-* rows on `supervisor/rulings-*` (docs only): take them in the next batch. Never edit S-* rows.
- 196 (#352, PR #402) needs the supervisor's ruling before merge: migration `delivery_0009_related_approvers`, isolation review inserting directly as `shifde_app` for every row of the requirement 4 table and mutating the guard (drop each IF), approver decision. S-056 is a design view only; any DDL beyond the spec plus addendum 1 goes back to the supervisor.
- 191 (#384, PR #393): new head `8276b8f` needs code and security review of the delta, the approver, full gates and CI on the exact head; it is at `review` round 2 with no reviewer started. The approver traced that no test server starts inside an API or worker process. The trigger is McpTestServer (decision.md), not RestTestServer.
- A second failure of `test_a_worker_at_its_abandoned_cap_stops_claiming_and_exits_1` (#401) on any head goes to the supervisor before any merge; same for `test_orphaned_holds` (#366). No rerun-and-merge on a red CI: send the FAILED lines first.
- Merge only with full gates and green CI on the head that merges (`--match-head-commit`, merge lock). After a merge with a migration: "hosted DB setup due" to the supervisor, no full gate active.
- If `scripts/supervisor/autostart.ps1` shows as deleted, neither commit nor restore it; tell the supervisor.

## Exact next step
1. `factory/bin/job queue` (takes about 2 minutes; run in the background).
2. **191** at review r2: spawn reviewer-code and reviewer-security (opus) on the delta since `0478201`. Then approver.
   All factory gates below are PASS; none of their reviewers has started (round 1 unless noted). Each needs reviewer-code, reviewer-security (opus) and an isolation-reviewer (all four are isolation-critical), except 191 (code and security only):
   - **193** (#290 Fabric, PR #404, `c541744`, main merged in): rule on B-4107 (setup-only change of `test_sp8_targets_of_one_organization_share_one_claim[fabric]`, distinct item names because requirement 2 refuses equal ones; lead leans accept, as for B-4129).
   - **194** (#292 Palantir, PR #405, `3c4def8`): `plan_details` takes the sealed plan's toolchain digest; target PATCH left out (#397).
   - **195** (#294 Docker, PR #403, `48b4d5b`): B-4129 setup-only test change; check the Snowflake branch edit in `PlanService.request` and the render dispatch default.
   - **196** (#352, PR #402, `1faf7eb`): direct-INSERT table and one-IF-at-a-time mutations (see Rulings); one surviving mutant (tenant predicate of `api_key_creator`, redundant under RLS).
3. Start **#390** (unblocked by 192), **#394** (after 189, also the dashboard bind limit), **#398** (after 190), **#401**; **#397** after 194; **#295**, **#296** last. #395 is the owner's: no factory label. Slots: concurrency 8.
4. **Batch 7** (`lead/batch-7` from origin/main): whichever of 191, 193-196 are approved, and any `supervisor/rulings-*`. 193/194/195 edit the same shared files (`delivery/plans.py`, `executor/runner.py`, `delivery/schemas.py`, `CLAUDE.md`): expect conflicts beyond DECISIONS.md (a conflict outside DECISIONS.md goes to the supervisor; merge them one at a time). 196 changes the signatures of `repository.insert_plan`/`insert_approval` (a `requested_api_key_id`/`decided_api_key_id` argument, None allowed): 194 and 195 may call them directly.
   `backend/tests/delivery/test_release_gate.py` (strict-subset assertion) breaks for whichever of 193/194/195 merges last.

Next free lead decision id: **B-4131** (4098-4100 job 191, 4104-4109 job 193, 4113-4115 job 196, 4120-4124 job 194, 4125-4129 job 195, 4130 lead; older unused: B-4038, B-4039, B-4061, B-4062, B-4064, B-4066, B-4075, B-4077 to B-4080, B-4087, B-4088, 4110-4112, 4116-4119).

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
