# Handoff — 2026-11-18 00:20, charter 7 factory run (lead, session 0f98da4a)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress"), then this file. Older handoff: `e528bb6:docs/build/HANDOFF.md`.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` (plus this handoff commit).

## What was just finished

- Merged: PR #35 (job 005, #20 SP2-2 delivery targets, 13/13, `06878e1`), PR #29 (job 001, #1 F-03, `5ac5ea9`, B-2560), PR #37 (job 002, #2 J-M2, isolation review APPROVED, lead yaml entry, 13/13, `978ca66`, B-2563).
- `factory/bin/job queue` fix for hand-merged jobs (B-2561, `19c06ce`).
- Follow-up #39 filed (tenants settings key trigger + DB-level grant test).

## Exact next step

1. `/factory run`. `factory/bin/job queue` should show `resume 4 007-f-06-tool-invocation review round 3`. Spawn in one message `reviewer-code` (model sonnet) and `reviewer-security` (model opus) with the job header and `Review round 3.` Then all PASS → `job advance 007 approve`, spawn `approver` (`PR: #38. Final round: 3.`), then `factory/bin/job merge 007-f-06-tool-invocation`. Round 3 is the last round: any CHANGES → needs-human; then the lead fixes via `job feedback 4`.
   - Job 007 touches `runtime/steps.py` (audit events, no migration). Charter 4 lists runtime as critical: before merge, have `isolation-reviewer` read PR #38 (the factory's security reviewer is not the isolation review).
2. Then the queue at concurrency 1: #5 onward, SP2-3..SP2-10 (#21-#28), #33, #34, #36, #39.

## Open branches and worktrees

- `.worktrees/007-f-06-tool-invocation` on `factory/007-f-06-tool-invocation` @ `36d32eb` (pushed, clean), PR #38 draft.
- Merged job worktrees `001-*`, `002-*` and gate checkouts `gate-001`, `gate-002`, `gate-004`, `gate-005`: safe to remove (`git worktree remove --force`; pnpm node_modules makes it slow — run in the background).

## Traps

- Do not run two full gates (or a factory gate beside a `gates.py` run) at once: under load the full suite takes 50+ min (gates.py run hit the 60-min background limit), `test_p8_perf` and DB connect timeouts flake, and `next build` failed twice with "Another next build process is already running" (B-2562). Give `gates.py` background runs `timeout` 7200000.
- Tell builders not to run the full backend suite (they stall waiting on it); the lead runs `scripts/gates.py` at merge.
- `factory/bin/job queue` can take 10+ minutes when it removes a merged job's worktree; run it in the background. If a `jobdb drop` hangs holding the factory lock, kill it and rerun `python factory/bin/jobdb drop <job>`.
- Protected-path entries (`deliberate_changes.yaml`, definer allow-list) are added by the lead on the PR branch after isolation review (B-2530); never `test_rls_schema.py`.
- DECISIONS numbers: lead used B-2560..B-2563; job 007 uses B-2540; job 002 used B-2550. Give each new builder a free range.
