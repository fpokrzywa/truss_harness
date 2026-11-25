# Handoff — 2026-11-24 20:45 EST, charter 7 factory run (lead session cd676343), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running; no agent running at handoff. Merged this session: 179 (#363, `e3190da`, B-4037). Main is `e3190da` plus docs commits; CI on main `e3190da` was not checked.

## Rulings
- The supervising session f18944f0 (`agt-trus-harness-ad`) handed off at 19:30; its successor had not introduced itself when this was written. Hold new rulings until it does. Work that needs no ruling goes on.
- Waiting for a ruling: **173** (S-044: synced head with full gates and green CI, plus the diff against `155d4c30`); **183** (approver ESCALATE: `scripts/traceability.py` gains rows for F-5.3-05 and F-5.3-11; decision.md recommends merge); **176** after its round 2 (S-041 covered only the patch at `489bf9af`; its fix is outside it); **180** after round 2 (adapter verify and executor job result contract, B-4044/B-4045). 177 (`authz`) and 181 may escalate too.
- Escalations are ruled on by the supervisor, not the lead. The lead may send a job back with `job feedback`.
- After 173 and after 176 merge: "hosted DB setup due". Tell the supervisor; go on with the next gate only as it says.
- Merge only with green CI on the head that merges: sync and push first, wait for CI, then `job merge` (or the hand merge with `--match-head-commit`).
- A second `test_orphaned_holds` failure away from 00:00 UTC: stop and tell the supervisor (#366).
- Never skip, loosen or add retries to a test. Isolation review APPROVED does not settle that: in 184 the loosening was in a note.
- If `scripts/supervisor/autostart.ps1` shows as deleted, neither commit the deletion nor restore it; tell the supervisor.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`, then resume:
   - **180** (#273, PR #370) build round 2: new builder, `feedback-1.md` (fixed detail for `ReleaseNotRenderable`). Then gate, reviews, approver, ruling. Decide requirement 21's `CLAUDE.md` sentence (wording in build.md).
   - **184** (#366, PR #367) build round 2: new builder, `feedback-1.md` (restore the 6 loosened tests). Isolation review 2 after it.
   - **176** (#351, PR #356) build round 2: new builder, `feedback-1.md`. Head `6c1572af` failed one test in the full suite (`176-merge` 17/18) and its CI run failed (log not read: check it is the same test). Then full `176-merge` gates, green CI, new ruling.
   - **177** (#359, PR #372, `e90db68`) at review: spawn reviewer-code and reviewer-security (opus) and an isolation review (authz). Its builder stalled after READY; the worktree was clean and pushed, gate PASS on `e90db68`.
   - **181** (#358, PR #369, `3ccfd5e`) at gate: run `factory/bin/gate`. Check the three tests whose input changed (build.md). Its builder reported two older publish/run-time differences (`select 1;1`, `:delete`): file an issue if none exists.
   - **182** (#360, PR #371, `b105a8b`) at approve, code and security PASS: isolation review first, then the approver.
2. **173** (#286, PR #354): sync, full `173-merge` gates, green CI, then ask for S-044.
3. New issues when slots are free: none is unblocked beyond these (see Dependencies).

Next free lead decision id: **B-4060** (B-4038, B-4039 also free). Given out: 177 B-4040..4043, 180 B-4044..4048, 181 B-4049..4051, 182 B-4052..4055, 183 B-4056..4057 (recorded), 184 B-4058..4059.

## Dependencies (job queue does not know them)
#274/#275 after #273 (#275 has two lead comments: Snowpark column-table re-check, and the version DEEP_HEALTH resolves to, S-043); #290, #292, #294, #352 need #286; #364 needs #351 (new migrations); #295, #296 last. #358 before any live Snowflake app; #360 item 4 (runbook line) before any deployment that enables `assist_ask`. When SP6, SP7 and SP8 finish, run each done-when demo and record it in STATUS.md (SP7's after 183 merges). `job queue` lists blocked issues as `new`: start only those whose dependency is merged.

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
