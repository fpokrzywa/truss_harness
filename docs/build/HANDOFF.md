# Handoff — 2026-11-24 22:00 EST, charter 7 factory run (lead session 30fa23c5), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running; no agent running at handoff. Nothing merged this session (every finished job waits for a ruling). Main is `e3190da` plus docs commits; CI on main `e3190da` was green.

## Rulings
- The former supervisor f18944f0 (`agt-trus-harness-ad`) confirmed at 21:12 that it no longer rules; its successor will introduce itself and the owner is being told. Hold every ruling until then. It has the list of 183, 182, 177, 173 and 176 in its own handoff file.
- Waiting for a ruling (each `factory/jobs/<job>/decision.md`):
  - **183** (#284, PR #368): `scripts/traceability.py` rows F-5.3-05, F-5.3-11; approver recommends merge; CI green on `2108c311` (older than main's docs commits: sync before merging).
  - **182** (#360, PR #371): synced at `940af55` (B-4052 reworded, decision.md item 3); isolation review 1 APPROVED; approver recommends merge; `182-merge` gates 18/18 on `940af55` (7012 passed, 10 skipped); CI green on it.
  - **177** (#359, PR #372): synced at `25e0d9c`; isolation review 1 APPROVED; approver recommends merge (rule: authz/tenancy); CI green on `25e0d9c`; full gates not run.
  - **176** (#351, PR #356): synced at `776f63b` (CI backend pending when written); round 2 is a test-only fix (B-4063), gate and both reviews PASS, approver ESCALATE by rule (migration `assist_0002`); needs full `176-merge` gates, green CI, a ruling.
  - **180** (#273, PR #370): synced at `3ea62df`, round 2 closed isolation finding 1 (approver reproduced the fix), gate and reviews PASS, CI green on `3ea62df`; approver recommends merge and asks to confirm the adapter verify and job result contract (B-4044, B-4045); full gates not run. After its merge the lead adds requirement 21's `CLAUDE.md` sentence (wording in build.md).
- **173** (#286, PR #354): synced at `911533d` (patch equal to `155d4c30` outside DECISIONS), but `173-merge` was 17/18: `test_the_generator_version_is_appgens` fails where #272 meets this branch. Sent back: build round 3, feedback-2. S-044 has to be asked on the new head. Round 3 READY at `f83be49` (Docker added to the render map and refused by name, B-4070, B-4071; 22 tests fail without it), at gate: run `factory/bin/gate 173-sp8-1-kinds-claims`, then reviews (code, security), an isolation review of `911533d..f83be49`, the approver, full `173-merge` gates, green CI, then S-044.
- Escalations are ruled on by the supervisor, not the lead. The lead may send a job back with `job feedback`.
- After 173 and after 176 merge: "hosted DB setup due". Tell the supervisor; go on with the next gate only as it says.
- Merge only with green CI on the head that merges: sync and push first, wait for CI, then `job merge` (or the hand merge with `--match-head-commit`).
- A second `test_orphaned_holds` failure away from 00:00 UTC: stop and tell the supervisor (#366).
- Never skip, loosen or add retries to a test. Isolation review APPROVED does not settle that.
- If `scripts/supervisor/autostart.ps1` shows as deleted, neither commit the deletion nor restore it; tell the supervisor.

- `S:	mp\HANDOFF.draft.md` is this session's stray draft (removal of `S:	mp` was refused; the folder holds older files): leave the folder alone.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`, then:
   - **184** (#366, PR #367, `8074343`) at approve, gate and code PASS: isolation review 2 first. It must itself re-run the mutations (unknown-usage charge 3 days back: origin/main 7 failing, round 1 one, now the same 7 plus the new test, as build.md claims): the code reviewer only read. Then the approver.
   - **181** (#358, PR #369): build round 2 READY at `2e47825` (isolation findings 1-3 and the security finding addressed; B-4067), at gate: run `factory/bin/gate`, then reviews, isolation review 2 (probe the same forms again), the approver. build.md lists newly refused harmless forms and spec lines that yield: the approver decides.
   - 173 as above; 180 and the others as above.
2. Full gates for the jobs a ruling allows, one at a time: 177, 176, 180, 173 and 183 have none on their synced heads.
3. New issues when slots are free: #373 (after #360), #374 (after #359), #375 (after #358). None is unblocked now.

Next free lead decision id: **B-4073** (B-4038, B-4039, B-4061, B-4062, B-4064, B-4066 also free). Given out: 180 B-4060..4062, 176 B-4063..4064, 184 B-4065..4066, 181 B-4067..4069, 173 B-4070..4072.
Isolation reviews saved and committed this session: `docs/build/reviews/177-isolation-review-1.md`, `181-isolation-review-1.md` (BLOCKED), `182-isolation-review-1.md`. Filed: #373, #374, #375.

## Dependencies (job queue does not know them)
#274/#275 after #273 (#275 has two lead comments: Snowpark column-table re-check, and the version DEEP_HEALTH resolves to, S-043); #290, #292, #294, #352 need #286; #364 needs #351 (new migrations); #373 needs #360; #374 needs #359; #375 needs #358; #295, #296 last. #358 before any live Snowflake app; #360 item 4 (runbook line) before any deployment that enables `assist_ask`. When SP6, SP7 and SP8 finish, run each done-when demo and record it in STATUS.md (SP7's after 183 merges). `job queue` lists blocked issues as `new`: start only those whose dependency is merged.

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
