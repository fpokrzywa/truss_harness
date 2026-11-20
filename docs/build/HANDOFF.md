# Handoff — 2026-11-20 13:35, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress", the SP3 row of "Sub-projects 2 to 8"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` after this commit. Local cluster on 55450 running. No open PRs. Jobs 029, 030, 031 merged.

## What was just finished (this session)

- #78 SP3-1 (job 029, PR #87) merged `4971fc3`; #82 SP3-5 (job 030, PR #88) merged `2888b77`; #79 SP3-2 (job 031, PR #92) merged `4a7c643`. Isolation reviews in `docs/build/reviews/030-sp3-5-isolation-review-1.md`, `031-sp3-2-isolation-review-1.md`.
- Filed: #89 (main CI: executor tests write credentials files without mode 0600, 22 Linux failures), #90 (`test_traceability` import fails on Python 3.13), #91 (orphaned direct-invocation holds; rule id), #93 (release manifest redaction; retire guard without client context; `set_state` 409 mapping).
- DECISIONS B-2704 (venv Python trap, below), B-2705 (protected-yaml jobs handled by the lead).

## Exact next step

`/factory run 80` (SP3-3 evaluation; it must also close 029 review finding 3, dataset_id resolved within client and project), then 81, 83, 84, 85, 86 one at a time (`job queue` orders by issue number, so use `/factory run <n>`). Strongly consider `/factory run 90` and `/factory run 89` early: both are small and main CI is red until they merge. After #86: `python scripts/release_demo.py --fake` on main (SP3 done-when), record in STATUS, then the follow-ups with `/factory run`, then sub-project 4.

## Pattern that worked (B-2705)

- A job whose migration changes a ported SETH table (needs a `deliberate_changes.yaml` entry): builder lists the entry in build.md and reports READY with the schema test failing. Lead checks the entry against the migration, appends it on the job branch, merges `origin/main` into the branch, runs `uv run pytest tests/schema tests/identity/test_schema.py`, pushes. Then in parallel: `gates.py --ref <sha> --name <id>-merge` (background, ~32 min), `isolation-reviewer`, `reviewer-code` (sonnet), `reviewer-security` (opus) with the job header. All pass → `gh pr ready <pr>; gh pr merge <pr> --merge --match-head-commit <sha>`; then `job queue` marks merged. The factory gate itself always FAILs on the protected path, and `job advance needs-human` refuses while rounds remain, so do not route these through `factory/bin/gate`.
- Jobs without a protected change: normal factory flow.

## Traps

- **This session had `VIRTUAL_ENV=backend\.venv` set (B-2704)**: bare `python` was the venv's CPython 3.13.1, which fails `test_traceability`. Until #90 merges, run gates as `env -u VIRTUAL_ENV PATH="$(echo "$PATH" | tr ':' '\n' | grep -v '\.venv' | paste -sd:)" python scripts/gates.py ...` (same for `factory/bin/gate`).
- Never `cd` into a worktree in a Bash call; prefer `git -C`. Edit STATUS/DECISIONS with Python `newline=''`. Next free DECISIONS id **B-2706**.
- `factory/bin/job queue` takes 1-2 min. Every new route: classify in `test_workspace_routes.py` and add to the non-SETH list in `tests/app/test_routes.py`; a new head migration updates `tests/identity/test_schema.py` head assertions.
- Builders must not restart the cluster; never `-m live` or read `.devdata/`. No agent has a browser tool.
- The lead's context reaches ~175k after 3 jobs; hand off after 3.
