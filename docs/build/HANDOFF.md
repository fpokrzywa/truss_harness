# Handoff — 2026-11-25 ~00:00 EST, charter 7 factory run (lead session fa50758a), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running; no agent, gate or wait loop running at handoff. Merged this session: 176 (#351, PR #356, `44027c6`) under S-048: `gates.py --name 176-merge-r2` 18/18 on `4d854bf` (7125 passed, 10 skipped in 1597 s), CI green on that head, patch-equal to `776f63b` except DECISIONS.md, `--match-head-commit`.

## Rulings
- **Hosted database setup is due** (176 carries migration assist_0002). Start no full `gates.py` run until a supervisor has answered that. Factory gates (`factory/bin/gate`) ran under this rule before the merge only.
- No supervisor is up. agt-trus-harness-e6 (cee9405c) said at 23:16 that it handed off and does not rule; its successor has not introduced itself. Its words are in `new_session_handoff.md` and its journal. S-049 is the next free S id; never edit S-* rows (it noted the S-044 row overstates what it read; the successor corrects it).
- **181** (#358, PR #369, `101c0e4`), needs-human: gate, code, security PASS, isolation review 3 APPROVED (`docs/build/reviews/181-isolation-review-3.md`), CI green on `101c0e4`. The approver found no defect and escalates only because `sqlapi.check_read_only` refuses more reads (adapter outbound behaviour). Needs a ruling, then sync onto main, full gates and CI. Follow-up posted on #375.
- **177** (#359, PR #372, `25e0d9c`), needs-human: packet in `factory/jobs/177-a-project-scoped-api/ruling-packet/` (README, the keep-both file, both diffs; made against main f49addf, 176 also edits that test file: re-make it on today's main). Not run, not applied, not pushed.
- **173** (#286, PR #354, `f83be49`), needs-human: packet in `factory/jobs/173-sp8-1-kinds-claims/ruling-packet/README.md` (the 12 + 3 cases, conflicts; CLAUDE.md is a new one). Isolation review 3 covers `f83be49` only. Then `job feedback 286`.
- Escalations are ruled on by the supervisor. The lead may send a job back with `job feedback`. Conflict outside DECISIONS.md goes back to the supervisor before merging.
- Merge only with green CI on the head that merges: sync and push first, wait for CI, then `job merge` (or the hand merge with `--match-head-commit`).
- A second `test_orphaned_holds` failure away from 00:00 UTC: stop and tell the supervisor (#366).
- Never skip, loosen or add retries to a test. Isolation review APPROVED does not settle that.
- If `scripts/supervisor/autostart.ps1` shows as deleted, neither commit the deletion nor restore it; tell the supervisor.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`, then:
   - **185** (#274 SP6-7, PR #378, `6b4b8a3`) at approve: gate, code, ux, ui PASS (no reviewer saw a rendered page). Approver, then `job merge` if APPROVE (its sync conflicts in DECISIONS.md are likely).
   - **187** (#373, PR #380, `c0226a9`) at review: gate PASS. Spawn code and security (opus).
   - **188** (#377, PR #379, `5845399`) at approve: gate, code, security PASS, isolation review 1 APPROVED (`docs/build/reviews/188-isolation-review-1.md`: catches all base catches plus an A-into-B export leak base missed). Approver with that file, then `job merge` if APPROVE. Not yet filed: its observation that summary and reconciliation are read only as A (pre-existing).
   - **184** (#366, PR #367, `e714b60`) at gate, round 3 (feedback-2.md: window from the test's own budget). Run the gate, code (sonnet), then isolation review 3: repeat the review 2 mutations on base and head. The builder measured 12/13 failures for "unknown charge to another client" where review 2 measured 5: its explanation (its plugin also moves run-hold charges) is inferred, so have the reviewer check it.
   - **186** (#275 SP6-8, PR #381, `a313f49`) at gate, round 1: builder reports `SNOWFLAKE DEMO PASSED`, `SOLUTION DEMO PASSED`, checks.sh exit 0; live test skipped. Run the gate and code review. Run demos with the main checkout's `backend/.venv` off PATH (else they import its `shifde`). After the merge run the SP6 done-when demo on main and record it.
2. Blocked by rulings: 173, 177, 181, the hosted database step. After 176, #364 is unblocked (new migrations); #374 needs #359; #375 needs #358.

Next free lead decision id: **B-4085** (B-4075, B-4077, B-4078, B-4079, B-4080 unused; B-4073 was already taken on main before this session; also free: B-4038, B-4039, B-4061, B-4062, B-4064, B-4066). Given out: 184 B-4065..4066, 181 B-4067..4069, 173 B-4070..4072, 185 B-4074, 187 B-4076, 186 B-4081..4084.

## Incident to know about
The 188 builder ran `rm -f /tmp/*188*` in the shared user temp folder at about 23:35 EST (`C:/Users/FreddiePokrzywa/AppData/Local/Temp`). Top-level files with "188" in the name are gone and unlisted; directories were kept. The 176 gates that were running finished 18/18. If any other program reports a missing temp file from then, this is the likely cause. It also left `checks.sh` children running until 23:36. Brief agents: delete only your own files, by exact path.

## Dependencies (job queue does not know them)
#290, #292, #294, #352 need #286; #294 also takes 173 review 3 finding 2 (render must dispatch by kind with a refusing default) and note 3; #374 needs #359; #375 needs #358; #295, #296 last. #358 before any live Snowflake app. When SP6 and SP8 finish, run each done-when demo and record it in STATUS.md. `job queue` lists blocked issues as `new`: start only those whose dependency is merged.

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
