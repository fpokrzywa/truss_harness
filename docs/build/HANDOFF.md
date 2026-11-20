# Handoff — 2026-11-19 19:35, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress" and "Sub-projects 2 to 8"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` after this commit. Local cluster on 55450 running (started from the main checkout).

## What was just finished (this session)

- Job 026 #26 SP2-8 Deliver nav, targets and executors screens: PR #75, `job merge` `19e12f3`; lead `026-merge` 13/13 (3829 passed).
- Job 027 #27 SP2-9 plans, plan approvals, deployments screens: PR #76, `job merge` `257b3c2`; lead `027-merge` 13/13.
- Job 028 #28 SP2-10 live test (opt-in `-m live` + `.devdata/databricks.env`), runbook `docs/runbooks/databricks-live-validation.md`, `scripts/delivery_demo.py --fake`: PR #77 after 3 rounds, `job merge` `84aae36`; lead `028-merge-r2` 13/13 (3855 passed, 1 skipped).
- **Sub-project 2 done-when** run on main `84aae36`: `python scripts/delivery_demo.py --fake` → `DELIVERY DEMO PASSED`. Recorded in STATUS as met against the fake platform, live validation waiting on owner input (B-2697).

## Exact next step

Sub-project 3, charter 7 step 1: read the main design (section 6 table and sub-project 3's "done when"), write `docs/superpowers/specs/2026-11-19-subproject-3-<name>-design.md`, have a fresh agent read it as a builder and list what it would have to guess, settle and log those (DECISIONS next free id **B-2698**), cut it into `factory` issues (one module-sized slice each, acceptance checks as commands, label `sp3`), then `/factory run`. Note the queue already holds follow-ups and known defects (#33..#74); they run in issue-number order after new issues only if numbered lower, so consider whether SP3 issues should go first (`job queue` orders by issue number).

## Pattern that worked (each job ~1-2 h)

- Front-end / scripts jobs: approver APPROVEs, `factory/bin/job merge <id>` merges (it re-runs `checks.sh`, ~6-10 min: run it with `run_in_background` and a long timeout; a 10-min foreground timeout kills it, then just re-run it, the job stays in `merging`).
- Start `python scripts/gates.py --ref <head sha> --name <id>-merge` (background, timeout 90 min) as soon as the factory gate passes; full suite takes 33-48 min when another gate runs alongside. If a review asks for changes, `TaskStop` it and restart on the new head.
- Isolation-critical/backend jobs: also spawn `isolation-reviewer`; approver escalates migrations/credentials; merge by hand after 13/13 (`gh pr merge <pr> --merge --match-head-commit <sha>`), `job queue` marks merged.
- Flake #51 precedent (B-2631/B-2680) still applies.

## Open branches and worktrees

- Job worktrees `.worktrees/026-*`, `027-*`, `028-*` and gate checkouts `*-merge*` are merged/detached, safe to remove (`git worktree remove`).
- No open PRs from this session.

## Traps

- Never `cd` into a worktree in a Bash call; prefer `git -C`.
- `factory/bin/job queue` takes 1-2 min. `factory/bin/gate` needs the full job id.
- Edit DECISIONS/STATUS with Python `newline=''`.
- Every new route: classify in `backend/tests/authz/scoping/test_workspace_routes.py`; say so in spec-writer and builder prompts.
- Builders must not restart the cluster (B-2632); builders must never run `-m live` or read `.devdata/`.
- No agent has a browser tool (`agent-browser` not installed): UI/UX reviews are from diff + component tests only.
