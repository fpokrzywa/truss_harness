# Handoff — 2026-11-21 09:55 UTC, charter 7 factory run (lead session 4e057a99)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress", the SP3 row of "Sub-projects 2 to 8"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` after this commit. Local cluster on 55450 running. No open PRs. Factory `concurrency` is 10 (S-014). Jobs 040-042 are `merged`; worktrees `.worktrees/04[0-2]-*` and `.worktrees/041-merge` are finished and can be removed.

## What was just finished (this session)

- #96 (job 040, PR #105): `/api/metrics` ops-stats cache moved from module globals to `app.state` (it was shared by every app in a test process, hiding a finished run on fast Linux CI); regression test `tests/app/test_metrics_ops_cache.py`. code PASS, approver APPROVE, `job merge`.
- #104 (job 042, PR #106): SP3 front-end follow-ups (groups pagination, archived seal hidden, duplicate pins block seal, release header, plan-dialog loading lines, read-only input style, no-datasets hint). code/ux/ui PASS, approver APPROVE, `job merge`.
- #99 (job 041, PR #107): plan responses null `release_id`, `release_digest`, `evaluation_run_id` for callers without release read; Release help line. All 4 reviews PASS; approver ESCALATE. Lead: isolation review 1 APPROVED (`docs/build/reviews/041-isolation-review-1.md`), `gates.py --name 041-merge` on `2b5b280` 12/13 (2 load flakes outside the diff, pass alone), merged `2d841f3`, decision B-2713.
- Filed #108 (isolation notes 5-6 of job 041) and #109 (the two load flakes).

## Exact next step

`/factory run` the older follow-ups that `factory/bin/job queue` lists as `new` (#33, #34, #36, #39, #41, #44, #46, #47, #51, #55, then #56 onward, #108, #109), three at a time, keeping one hand merge at a time. Then sub-project 4 per charter 7 steps 1-2: write its short design in `docs/superpowers/specs/` (design table row 4, go-live), have a fresh agent list what it would have to guess, settle and log (next DECISIONS id **B-2714**), cut issues labeled `factory` + `sp4`. All major audit findings must be closed or recorded as needing the owner's written acceptance by the end of SP4.

## Pattern that worked (B-2705)

- A job whose migration changes a ported SETH table (needs a `deliberate_changes.yaml` entry): builder lists the entry in build.md and reports READY with the schema test failing. Lead checks the entry against the migration, appends it on the job branch, merges `origin/main` into the branch, runs `uv run pytest tests/schema tests/identity/test_schema.py`, pushes. Then in parallel: `gates.py --ref <sha> --name <id>-merge` (background, ~35-42 min), `isolation-reviewer`, `reviewer-code`, `reviewer-security` (opus) with the job header. All pass → `gh pr ready <pr>; gh pr merge <pr> --merge --match-head-commit <sha>`; then `job queue` marks merged. The factory gate itself FAILs on the protected path, so do not route these through `factory/bin/gate`.
- An escalated job without a protected change (e.g. 038): `job advance needs-human`, then the same lead trio (gates.py 13/13, isolation-reviewer, reviewer-security) and `gh pr merge --match-head-commit`.
- Jobs without escalation: normal factory flow, `factory/bin/job merge`.

## Traps

- Run factory scripts and gates.py with `PYTHONUTF8=1`, `VIRTUAL_ENV` unset and `.venv` removed from PATH: `export PYTHONUTF8=1; unset VIRTUAL_ENV; export PATH="$(echo "$PATH" | tr ':' '\n' | grep -v '\.venv' | paste -sd:)"` (B-2704; `job` crashes under cp1252 otherwise).
- `isolation-reviewer` is read-only and cannot write its file; the lead writes `docs/build/reviews/<id>-isolation-review-<n>.md` from its report. `reviewer-ui` sometimes returns text without writing its file; tell it in the prompt to write `round-<N>/review-ui.md` itself (works).
- Never `cd` into a worktree in a Bash call; prefer `git -C`. Edit STATUS/DECISIONS with Python `newline=''`.
- Approver always escalates a new migration and identity/auth UI.
- Every new route: classify in `test_workspace_routes.py` and add to the non-SETH list in `tests/app/test_routes.py`; a new head migration updates `tests/identity/test_schema.py` head assertions.
- Builders must not restart the cluster; never `-m live` or read `.devdata/`. No agent has a browser tool (`agent-browser` not installed), so UI is reviewed from diffs and component tests.
- The lead's context reaches ~140k after 3 jobs; hand off after 3.
- `factory/bin/job queue` takes ~2-3 min (it scans every subagent transcript for cost) and crashes on cp1252 without `PYTHONUTF8=1`; run it in the background.
- `reviewer-code` (sonnet) and `reviewer-ui` sometimes claim they have no Write tool; resume them with SendMessage saying the review file is their required output and they write it. Never write a factory review yourself.
- Full-suite gates on a hand merge can hit load flakes outside the diff; rerun the failed tests alone and, if they pass, merge per B-2631/B-2713 and file an issue (#109 is open for two of them).
