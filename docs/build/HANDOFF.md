# Handoff — 2026-11-18 16:50, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main`. Local cluster on 55450 running (started from the main checkout).

## What was just finished

- Job 013 (#11 F-17: bandit SAST, coverage floor 92%, OpenAPI snapshot diff in CI) merged as PR #49, `d546afb`, after lead isolation review APPROVED and `gates.py --name 013-merge` 13/13. Main CI green with the new steps (run 44387589036).
- Job 014 (#12 F-21: Playwright `pnpm e2e`, local only) merged as PR #50, `fc8ab5e`, after lead isolation review APPROVED and `014-merge` 12/13 (one flake in an unchanged backend; B-2631; filed as #51).

## Exact next step

1. `/factory run`. Queue at concurrency 1: next `new` line is #13 (F-16 API latency), then #14..#18, SP2-3..SP2-10 (#21-#28), #33, #34, #36, #39, #41, #44, #46, #47, #51.
2. Charter 6 concurrency count: 013 merged clean (13/13); 014 had a failed merge gate (flake), so the five-in-a-row count restarts after 014.

## Open branches and worktrees

- `.worktrees/013-merge` and `.worktrees/014-merge` are gate checkouts (detached, safe to remove with `git worktree remove --force`). Older merged job worktrees from 001-012 likewise.

## Traps (new this session, plus older ones)

- **Escalations for CI config or new dependencies are routine.** The approver escalates any change to `.github/workflows` or any new dependency. As the human: `job advance <id> needs-human`, run `gates.py --ref <head sha> --name <id>-merge` (full suite, ~30 min) and an `isolation-reviewer` in parallel, save the review to the job folder as `isolation-review-1.md`, then `gh pr merge <pr> --merge --match-head-commit <sha>`; `job queue` then marks the job `merged`.
- **A builder that restarts the cluster from its worktree pins that folder** (postgres cwd) and `job queue` crashes with WinError 32 removing it. Fix: `dev_db.py stop`, `rmdir` the empty folder, `dev_db.py start` from the main checkout (B-2632). Tell builders not to restart the cluster.
- Reviewers sometimes cannot write their file and reply with its content: save it yourself to `round-N/review-<r>.md`, then `job sync`.
- The factory gate (`checks.sh`) runs only a core subset of backend tests; before merging anything that changes shared behaviour, run `gates.py` (13 gates) yourself.
- An approver's APPROVE cannot be overridden through `job advance`; respawn the approver with the new evidence.
- `factory/bin/job queue` takes 1-2 minutes; give it a 300 s timeout. `dev_db.py start` can take >2 min.
- Never `cd` into a job or gate worktree in the main session (it changes the session cwd); use `git -C` and paths.
- Do not run two full gates at once. DECISIONS next free id: B-2633.
