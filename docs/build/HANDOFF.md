# Handoff — 2026-11-17, unit `goal1-final` (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Older handoffs: `a706dfd:docs/build/HANDOFF.md` and the `HANDOFF-*.md` files.

**State:** no agent and no gate run is running; no merge is half done; no resource or merge lock is held (`python scripts/unit.py locks` is empty); `main` = `origin/main` (the commit carrying this file).

## What was just finished

- Unit `goal1-final` is done (`unit.py done` printed `UNIT DONE`). Every SETH module in the design's layer order is `done` in STATUS.
- Software factory installed (charter 6) from zazencodes `94f7ae7`, GPLv3 (B-2480, B-2481). Branch `factory-install` (`80355a3`); merge commit `2905331` fast-forwarded to main after 13/13 gates (`goal1-final-merge`).
- Final checks on a clean checkout of main `d8b5aae` (`gates.py --name goal1-final-check`): 13/13 pass; full backend suite 3183 passed in **1277.68 s**. RLS schema + 21 cross-client files + startup role check: 813 passed. `test_schema_vs_seth.py`: 11 passed. `bash factory/checks.sh` exit 0 (4 min 48 s, from the main checkout); `factory/bin/job queue` exit 0 (empty).
- `factory/dashboard.html` title edit ("Truss Hardness factory") was found on disk, not made by the lead; committed as-is in `84749c5`. The owner may want "Harness".

## Open items found

1. **Suite time over the design's 20-minute target.** `suite-speed` measured 541 s on an idle machine (B-2330); today's two full runs took 1235.6 s and 1277.7 s. It is not known whether machine load (other sessions, the scripts unittest gate) or test growth (3183 tests now) explains it.
2. **Fable spend.** `usage_report.py` shows $19.50 on `claude-fable-5-2` (80 requests); the charter forbids Fable for agents. Find which session/agent used it.
3. `factory/bin/jobdb ensure` / `drop` are untested until a real factory job runs.

## Exact next step

1. Time the backend suite on an otherwise idle machine: from a clean checkout of main, `cd backend; uv run pytest -n 8 -q` (nothing else running). If under 1200 s, record it in STATUS; if over, profile with `--durations=30` and speed up setup (never skip, deselect or delete a test). Record the result as a STATUS row.
2. Then charter 7: file one GitHub issue (label `factory`) per open known defect in STATUS, create the factory labels, and start sub-project 2 (short design, issues, `/factory run`).

## Worktrees

`.worktrees/factory-install` (branch `factory-install`, merged), `.worktrees/goal1-final-merge`, `.worktrees/goal1-final-check` (detached, gate checkouts) can be removed. Older worktrees listed by `git worktree list` are all merged.
