# Handoff — 2026-11-19 19:55, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress" and "Sub-projects 2 to 8"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` after this commit. Local cluster on 55450 running (started from the main checkout). No open PRs.

## What was just finished (this session)

- Sub-project 3, charter 7 steps 1-2: short design `docs/superpowers/specs/2026-11-19-subproject-3-releases-evaluation-entra-design.md` (sections 1-11), a fresh builder read it and listed 35 guesses, all settled in its **section 12** (binding; B-2698..B-2701). Cut into 9 issues **#78-#86** (labels `factory`, `sp3`), each with the section-10 checks as commands, `fc35b5b`.
- Removed the merged `.worktrees/0NN-merge*` gate checkouts (clean, ignored build output only).

## Exact next step

Charter 7 step 3 for sub-project 3: `/factory run 78`, then 79, 80, 81 (dependency chain 78 -> 79 -> 80 -> 81 -> 84 -> 85; 82 and 83 are independent; 86 last). Use `/factory run <n>` one issue at a time so SP3 goes ahead of the older follow-ups (#33..#74, `job queue` orders by issue number). After #86 merges: run `python scripts/release_demo.py --fake` on main (SP3 done-when), record it in STATUS (met for the evaluation gate; Entra met against the fake issuer, live validation waiting on owner input: no `.devdata/entra.env`), then drain the follow-ups with `/factory run`, then sub-project 4.

## Pattern that worked (each job ~1-2 h)

- Every SP3 backend issue adds a migration (protected path): the approver escalates; you run `python scripts/gates.py --ref <head sha> --name <id>-merge` (background, timeout 90 min; start it as soon as the factory gate passes), spawn `isolation-reviewer` on the PR diff, then merge by hand after 13/13 (`gh pr merge <pr> --merge --match-head-commit <sha>`); `job queue` marks merged. If the builder wrote schema-test entries into build.md (section 12 item 9), add them yourself on the branch before the 13-gate run.
- Front-end / scripts jobs: approver APPROVEs, `factory/bin/job merge <id>` (re-runs checks.sh, 6-10 min; run with `run_in_background`, long timeout).
- Flake #51 precedent (B-2631/B-2680) still applies.

## Traps

- Never `cd` into a worktree in a Bash call; prefer `git -C`.
- Bash heredocs containing apostrophes inside `$(...)`/quoted contexts break: write a Python script to a temp file and run it (that's how DECISIONS/STATUS/issues were edited this session). Edit DECISIONS/STATUS with Python `newline=''`. Next free DECISIONS id **B-2702**.
- `factory/bin/job queue` takes 1-2 min. `factory/bin/gate` needs the full job id.
- Every new route: classify in `backend/tests/authz/scoping/test_workspace_routes.py` with a builder entry; say so in spec-writer and builder prompts.
- Builders must not restart the cluster (B-2632); builders must never run `-m live` or read `.devdata/`.
- No agent has a browser tool: UI/UX reviews are from diff + component tests only.
- The lead's context hit 161k after writing the design; plan on handing off after roughly 2-3 factory jobs per session.
