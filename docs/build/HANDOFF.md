# Handoff — 2026-11-18 02:45, charter 7 factory run (lead, session after 0f98da4a)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress"), then this file. Older handoff: `4c0401c:docs/build/HANDOFF.md`.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` (plus this handoff commit).

## What was just finished

- Merged PR #38 (job 007, #4 F-06, `8494063`; isolation review `reviews/f06-isolation-review-1.md` APPROVED).
- Merged PR #40 (job 008, #5 F-13, `458c404`): approver escalated two product calls, lead decided them (B-2565), isolation review APPROVED (`reviews/f13-isolation-review-1.md`), `gates.py --name 008-merge` 13/13 (full suite 3460 passed in 1550 s, with a builder running alongside). Follow-ups filed as issue #41.
- `factory/checks.sh` retries `next build` only on "Another next build process is already running" (B-2564, `dbd6ccc`); the lock flake hit again with no other gate running.

## Exact next step

1. `/factory run`. Job `009-f-09-rest-adapter` (#6, PR #42) is in **review round 2** (gate PASS). In one message spawn `reviewer-code` (model sonnet) and `reviewer-security` (model opus) with the job header and `Review round 2. Write your review to round-2/review-<r>.md`, plus `isolation-reviewer` on PR #42 telling it round 1 was BLOCKED (`factory/jobs/009-f-09-rest-adapter/isolation-review-1.md`) and to confirm both findings are closed (C2 via `auth_method`-only PATCH; connections-level 403 tests with positive controls). Read-only; it replies inline, the lead saves it to `docs/build/reviews/f09-isolation-review-2.md` on the PR branch.
2. Job 009 went through `needs-human` + `job feedback 6`, so after reviews the approver may escalate again (adapter outbound change always escalates). If so: lead is the human (charter 7.5): `job advance 009 needs-human`, then in the worktree `git merge origin/main` (DECISIONS conflicts: keep both sides, ascending ids), push, `python scripts/gates.py --ref <full sha> --name 009-merge` (background, timeout 7200000), then `gh pr merge 42 --merge`, then `factory/bin/job queue` records it merged. Update the F-09 row in STATUS to closed.
3. Then the queue at concurrency 1: #8 onward (known defects), SP2-3..SP2-10 (#21-#28), #33, #34, #36, #39, #41.

## Open branches and worktrees

- `.worktrees/009-f-09-rest-adapter` on `factory/009-f-09-rest-adapter` @ `582eb7e` (pushed, clean), PR #42 draft/ready.
- `.worktrees/008-f-13-per-tool`, `.worktrees/008-merge` (gate checkout): merged, safe to remove in the background (`git worktree remove --force`).

## Traps

- The approver only sees factory reviews: when the lead's isolation review BLOCKS, save it in the job folder and tell the approver to read it; it then ESCALATEs and `job advance <id> needs-human` + `job feedback <n>` start the next round with a fresh budget.
- Reviewers sometimes reply inline without writing `round-N/review-<r>.md`; say in the prompt where to write it.
- Do not run two full gates at once; builders run only their module tests (say so in the brief). Give each builder a DECISIONS range: used so far B-2564, B-2565 (lead), B-2570 (job 008), B-2580, B-2581 (job 009). Next free: B-2566.. for the lead, B-2590.. for job 010.
- A session whose cwd is inside a job worktree blocks `job merge` from removing it (WinError 32); `cd` back to the main checkout before merging.
