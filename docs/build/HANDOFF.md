# Handoff — 2026-11-20 15:20, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress", the SP3 row of "Sub-projects 2 to 8"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` after this commit. Local cluster on 55450 running. No open PRs.

## What was just finished (this session)

- #90 (job 032, PR #94): approver escalated (`scripts/`), isolation review APPROVED (`reviews/032-isolation-review-1.md`), `gates.py --name 032-merge` 13/13 (3922 passed); the PR was merged by hand under the owner's account (`bd4f1a9`) while the gates ran. Job 032 is `needs-human` in the factory; `job queue` should mark it merged.
- #89 (job 033, PR #95): normal factory flow, `job merge` `5915c1c`. Linux CI's 22 executor failures are gone.
- Filed #96 (factory): `test_p8_observability` runs_finished_window gauge missing on Linux CI (pre-existing; the last red test on main CI).
- Job 034 (#80 SP3-3 evaluation): intake ACCEPT, route = code sonnet + security opus, spec written and amended with lead calls B-2706 (per-case cap = threshold/N), B-2707 (no matching model -> `error`), B-2708 (409 `evaluation_requires_full_capture`). 029 review finding 3 is spec acceptance criterion 5.

## Exact next step

`/factory run 80`: job 034 is at stage `build`, round 1, with no builder spawned yet. Spawn `builder` named `builder-034-sp3-3-evaluation-datasets`, port 5440, "Build round 1. First round: build the spec." The spec says the new tables' schema-test entries go in build.md for the lead to add (B-2530/B-2705 pattern below): `backend/tests/schema/*` is protected, so this job will likely need the lead's hand merge. Also run `isolation-reviewer` (new client-owned tables). Then #81, #83, #84, #85, #86 one at a time, then #96. After #86: `python scripts/release_demo.py --fake` on main (SP3 done-when), record in STATUS, then the follow-ups with `/factory run`, then sub-project 4.

## Pattern that worked (B-2705)

- A job whose migration changes a ported SETH table (needs a `deliberate_changes.yaml` entry): builder lists the entry in build.md and reports READY with the schema test failing. Lead checks the entry against the migration, appends it on the job branch, merges `origin/main` into the branch, runs `uv run pytest tests/schema tests/identity/test_schema.py`, pushes. Then in parallel: `gates.py --ref <sha> --name <id>-merge` (background, ~32 min), `isolation-reviewer`, `reviewer-code` (sonnet), `reviewer-security` (opus) with the job header. All pass → `gh pr ready <pr>; gh pr merge <pr> --merge --match-head-commit <sha>`; then `job queue` marks merged. The factory gate itself always FAILs on the protected path, and `job advance needs-human` refuses while rounds remain, so do not route these through `factory/bin/gate`.
- Jobs without a protected change: normal factory flow.

## Traps

- **This session had `VIRTUAL_ENV=backend\.venv` set (B-2704)**: bare `python` was the venv's CPython 3.13.1, which fails `test_traceability`. #90 is merged, but bare `python` in a venv shell is still 3.13.1; the factory scripts were run here as `env -u VIRTUAL_ENV PATH="$(echo "$PATH" | tr ':' '\n' | grep -v '\.venv' | paste -sd:)" python scripts/gates.py ...` (same for `factory/bin/gate`).
- Never `cd` into a worktree in a Bash call; prefer `git -C`. Edit STATUS/DECISIONS with Python `newline=''`. Next free DECISIONS id **B-2709**.
- `factory/bin/job queue` takes 1-2 min. Every new route: classify in `test_workspace_routes.py` and add to the non-SETH list in `tests/app/test_routes.py`; a new head migration updates `tests/identity/test_schema.py` head assertions.
- Builders must not restart the cluster; never `-m live` or read `.devdata/`. No agent has a browser tool.
- The lead's context reaches ~175k after 3 jobs; hand off after 3.
