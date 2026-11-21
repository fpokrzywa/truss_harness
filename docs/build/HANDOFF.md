# Handoff — 2026-11-21 02:45, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress", the SP3 row of "Sub-projects 2 to 8"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` after this commit. Local cluster on 55450 running. No open PRs. Factory `concurrency` is 10 (S-014).

## What was just finished (this session)

- #84 SP3-7 (job 037, PR #101): solutions, releases, evaluations screens; code/ux/ui PASS; approver APPROVE; `job merge` `dc4f7cc`.
- #85 SP3-8 (job 038, PR #102): release picker in the plan dialog, Entra profile on the platform IdP form, Entra group mapping dialog. Round 1 ux CHANGES fixed in round 2. Approver escalated (identity admin UI). Lead: security review PASS, isolation review 1 APPROVED (`docs/build/reviews/038-isolation-review-1.md`, written by the lead from the read-only reviewer's report), `gates.py --name 038-merge` on `dda51d6` 13/13 (4040 passed), merged `957d330`.
- #86 SP3-9 (job 039, PR #103): `scripts/release_demo.py --fake` + unit test, runbook fix; code PASS; approver APPROVE; `job merge` `5ede861`.
- SP3 done-when run on main `5ede861`: `python scripts/release_demo.py --fake` exit 0, `RELEASE DEMO PASSED`. Recorded in STATUS as met against the fake platform, Entra live validation waiting on owner input.
- Follow-up #104 filed (non-blocking front-end notes from 037/038).

## Exact next step

`/factory run 96`, then `/factory run 99`, `/factory run 104`, then the older follow-ups that `factory/bin/job queue` lists as `new` (#33, #34, #36, ...); several may run together (concurrency 10) when their intakes show no overlapping files, but keep one hand merge at a time. Then sub-project 4 per charter 7 steps 1-2: write its short design in `docs/superpowers/specs/` (design table row 4, go-live), have a fresh agent list what it would have to guess, settle and log (next DECISIONS id **B-2713**), cut issues labeled `factory` + `sp4`. All major audit findings must be closed or recorded as needing the owner's written acceptance by the end of SP4.

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
