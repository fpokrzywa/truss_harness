# Handoff — 2026-11-18 21:15, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main`. Local cluster on 55450 running (started from the main checkout).

## What was just finished

- Job 015 (#13 F-16 latency harness, `tests/perf`, `docs/build/perf.md`) merged PR #52 `b3f8322` (clean, 13/13 via `job merge`).
- Job 016 (#14 F-23 traceability matrix, `scripts/traceability.py --check`) merged PR #53 `382db0b` after lead feedback round. Item 23 is only narrowed: SETH PRD ids still to be mapped (needs a follow-up issue; the SETH PRD is readable in the SETH repo at `3ba0e06`).
- Job 017 (#15 ARCH M11 egress control, PR #54): lead answered intake (B-2645..B-2648), isolation review 1 APPROVED (`factory/jobs/017-*/isolation-review-1.md`), `gates.py --name 017-merge` 12/13 on `9bd95b5` (one test refused by the new gate), feedback-1 sent, builder round 2 READY at `ba614ed`. Its note 2 filed as #55.

## Exact next step

1. Job 017 is at stage `build` round 2 with `build.md` STATUS: READY: `factory/bin/job advance 017-arch-m11-classification-x gate`, `factory/bin/gate 017-arch-m11-classification-x`, then review (code sonnet, security opus), approver. It will escalate again (access control): as the human, run `python scripts/gates.py --ref <head sha> --name 017-merge-r2` (13/13 needed), then `gh pr merge 54 --merge --match-head-commit <sha>`; `job queue` marks it merged. A fresh isolation review is only needed if round 2 touched `backend/src` (it touched tests only: `tests/app/test_p7_exec.py`, `tests/_support/p3util.py`, `p5util.py`).
2. Then `/factory run`: #16, #17, #18, SP2-3..SP2-10 (#21-#28), #33, #34, #36, #39, #41, #44, #46, #47, #51, #55.
3. File two follow-ups when convenient: F-23 SETH PRD id mapping; ARCH M11 `data:export` permission + ontology export route (protected migration: lead writes it outside the factory).
4. Concurrency: 015 merged clean; 016 and 017 needed lead rounds, so the five-in-a-row count restarts.

## Open branches and worktrees

- `.worktrees/017-arch-m11-classification-x` (job 017, live). `.worktrees/017-merge` and older `*-merge` gate checkouts are detached and safe to remove.

## Traps (new this session, plus older ones)

- **Escalations for CI config or new dependencies are routine.** The approver escalates any change to `.github/workflows` or any new dependency. As the human: `job advance <id> needs-human`, run `gates.py --ref <head sha> --name <id>-merge` (full suite, ~30 min) and an `isolation-reviewer` in parallel, save the review to the job folder as `isolation-review-1.md`, then `gh pr merge <pr> --merge --match-head-commit <sha>`; `job queue` then marks the job `merged`.
- **A builder that restarts the cluster from its worktree pins that folder** (postgres cwd) and `job queue` crashes with WinError 32 removing it. Fix: `dev_db.py stop`, `rmdir` the empty folder, `dev_db.py start` from the main checkout (B-2632). Tell builders not to restart the cluster.
- Reviewers sometimes cannot write their file and reply with its content: save it yourself to `round-N/review-<r>.md`, then `job sync`.
- The factory gate (`checks.sh`) runs only a core subset of backend tests; before merging anything that changes shared behaviour, run `gates.py` (13 gates) yourself.
- An approver's APPROVE cannot be overridden through `job advance`; respawn the approver with the new evidence.
- `factory/bin/job queue` takes 1-2 minutes; give it a 300 s timeout. `dev_db.py start` can take >2 min.
- Never `cd` into a job or gate worktree in the main session (it changes the session cwd); use `git -C` and paths.
- Do not run two full gates at once. DECISIONS next free id: B-2653 (job 017 uses up to B-2652 on its branch).
