# Handoff — 2026-11-18 12:40, charter 7 factory run (lead session agt-trus-harness-f3)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main`. Local cluster on 55450 running.

## What was just finished

- Job 012 (#10 ARCH M2, connection test on the worker) merged as PR #48, `63bb033`, after 3 rounds; details in STATUS. Item 21 narrowed, not closed (model checks, ontology syncs, discover/invoke still inline).

## Exact next step

1. `/factory run`. Queue at concurrency 1: #11..#18 (known defects), SP2-3..SP2-10 (#21-#28), #33, #34, #36, #39, #41, #44, #46, #47. Next `new` line will be #11.
2. Charter 6: concurrency may go to 2 after five issues in a row merge without a failed gate. Job 012 had a failed full-suite merge gate (round 2), so the count restarts after 012.

## Open branches and worktrees

- Many old `.worktrees/*` from goal 1 and jobs 001-011 are merged and safe to remove (background `git worktree remove --force`). `.worktrees/012-arch-m2-move-adapter` may remain if `job merge` could not remove it.

## Traps (new this session, plus older ones)

- **The factory gate (`checks.sh`) runs only a core subset of backend tests.** Before merging any job that changes shared helpers or behaviour other modules call, run `python scripts/gates.py --ref <sha> --name <id>-merge` (13 gates, full suite, ~30 min) yourself. Job 012 passed checks.sh but failed 7 `tests/app` tests in the full suite.
- An approver's APPROVE cannot be overridden through `job advance` (approve → build/needs-human refused unless decision.md says ESCALATE). Respawn the approver with the new evidence; it rewrites decision.md, then `job advance <id> needs-human` and `job feedback <issue>`.
- Reviewers (`reviewer-code` especially) often cannot write their file and reply with its content: save it yourself to `round-N/review-<r>.md`, then `job sync`.
- For a critical module the lead also runs `isolation-reviewer` during review, saves it to the job folder as `isolation-review-N.md` and on the branch as `docs/build/reviews/<slug>-isolation-review-N.md`, and tells the approver to read it.
- `factory/bin/job queue` takes 1-2 minutes; give it a 300 s timeout. `dev_db.py start` can take >2 min (run in background).
- Never `cd` into a job worktree in the main session (it changes the session cwd and blocks `job merge` removal); use `git -C`.
- Do not run two full gates at once. DECISIONS next free id: B-2615.
