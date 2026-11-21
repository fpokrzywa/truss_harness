# Handoff — 2026-11-20 23:25, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress", the SP3 row of "Sub-projects 2 to 8"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` after this commit. Local cluster on 55450 running. No open PRs. Factory `concurrency` is now 3 (supervisor S-012, at the owner's request).

## What was just finished (this session)

- Salvage of session 0d01e221 (B-2709): stash held only `reviews/034-isolation-review-1.md` (kept); 034 worktree's unpushed round-2 commit kept and finished.
- #80 (job 034, PR #97): round 2 fixed the regex-scoring finding; reviews PASS; approver escalated (migration); isolation reviews 1-2 APPROVED; 13/13 (3967 passed); merged by the lead `7036192`.
- #81 (job 035, PR #98): calls B-2710; all reviews PASS; approver escalated; isolation review APPROVED with one same-organization note, filed #99; 13/13 (3979 passed); merged by the lead `696c0fb`.
- #83 (job 036, PR #100): calls B-2711; lead added the `idp_configs` `deliberate_changes.yaml` entries (B-2530); isolation reviews 1 and 2 BLOCKED on Entra e-mail trust (cross-client link takeover), fixed in rounds 2-3 (B-2712: only `acct` = 0 without `#EXT#` proves a member; `email` needs `xms_edov` true); isolation review 3 APPROVED; 13/13 (4040 passed); merged by the lead `648e16d`. Entra live validation waits on owner input.

## Exact next step

`/factory run 84` (job not created yet), then #85, #86 (SP3, in order; with concurrency 3 they may run together if their intakes show no overlap, but keep one hand merge at a time). Then #96, #99. After #86: `python scripts/release_demo.py --fake` on main (SP3 done-when), record in STATUS, then the older follow-ups with `/factory run` (queue shows them as `new`: #33, #34, #36, ...), then sub-project 4. Note: plain `/factory run` now starts three of the oldest follow-ups first, not SP3.

## Pattern that worked (B-2705)

- A job whose migration changes a ported SETH table (needs a `deliberate_changes.yaml` entry): builder lists the entry in build.md and reports READY with the schema test failing. Lead checks the entry against the migration, appends it on the job branch, merges `origin/main` into the branch, runs `uv run pytest tests/schema tests/identity/test_schema.py`, pushes. Then in parallel: `gates.py --ref <sha> --name <id>-merge` (background, ~32 min), `isolation-reviewer`, `reviewer-code` (sonnet), `reviewer-security` (opus) with the job header. All pass → `gh pr ready <pr>; gh pr merge <pr> --merge --match-head-commit <sha>`; then `job queue` marks merged. The factory gate itself always FAILs on the protected path, and `job advance needs-human` refuses while rounds remain, so do not route these through `factory/bin/gate`.
- Jobs without a protected change: normal factory flow.

## Traps

- **This session had `VIRTUAL_ENV=backend\.venv` set (B-2704)**: bare `python` was the venv's CPython 3.13.1, which fails `test_traceability`. #90 is merged, but bare `python` in a venv shell is still 3.13.1; the factory scripts were run here as `env -u VIRTUAL_ENV PATH="$(echo "$PATH" | tr ':' '\n' | grep -v '\.venv' | paste -sd:)" python scripts/gates.py ...` (same for `factory/bin/gate`).
- Never `cd` into a worktree in a Bash call; prefer `git -C`. Edit STATUS/DECISIONS with Python `newline=''`. Next free DECISIONS id **B-2713**.
- Run `factory/bin/job` and `factory/bin/gate` with `PYTHONUTF8=1`: otherwise `job` crashes decoding a transcript under cp1252 (`cost_usd`).
- Approver always escalates a job with a new migration; then the lead runs `isolation-reviewer` + `gates.py` 13/13 and merges with `gh pr merge --match-head-commit`. For a job that needs a protected `tests/schema` entry, a new round needs `job advance gate` + `factory/bin/gate` (FAILs on the protected path) + `job advance build`.
- Some reviewers forget to write their file; resume them with SendMessage to write it.
- `factory/bin/job queue` takes 1-2 min. Every new route: classify in `test_workspace_routes.py` and add to the non-SETH list in `tests/app/test_routes.py`; a new head migration updates `tests/identity/test_schema.py` head assertions.
- Builders must not restart the cluster; never `-m live` or read `.devdata/`. No agent has a browser tool.
- The lead's context reaches ~175k after 3 jobs; hand off after 3.
