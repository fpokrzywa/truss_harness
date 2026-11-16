# Launching the unattended build

Two goals run the whole build. Each is pasted once into a Claude Code session started in this directory. The standing instructions for both are in `docs/build/CHARTER.md`; the goals only state what "finished" means, because a goal is limited to 4,000 characters and is judged by a small model that reads the conversation and cannot run commands.

## Before the first launch

Already in place as of 2026-11-13:

- The design is approved.
- `DATABASE_URL` is in `.env.local`, and `.gitignore` excludes it.
- The GitHub repository is private, empty, set as `origin`, and the signed-in `gh` account has admin rights.
- Claude Code is 2.1.322.
- `.claude/settings.json` stops the file tools writing to the three source repositories or reading Agent Factory's two secret files. It covers the file tools only; the charter's rule covers shell commands.

Still yours to do:

1. Read section 3 of the charter. Pasting goal 1 is your agreement to the authority it grants: pushing, merging its own pull requests, creating two roles (and a database if needed) on your PostgreSQL server, and designing later sub-projects without coming back to you.
2. Optional: add `.devdata/databricks.env`, `.devdata/entra.env` or `.devdata/snowflake.env` if you have that access. Without them those parts finish against simulated platforms and are recorded as waiting.

## How to start a session

```powershell
cd s:\Development_2026\shi_work\agt_trus_harness
claude --permission-mode auto --model opus
```

Auto mode removes the per-command prompts and the goal removes the per-turn prompts; both are needed for the build to run without you. Then paste one goal below.

If a session ends before the goal is met (a usage limit pauses and resumes by itself; an exhausted credit balance or an unavailable model clears the goal), start a session the same way and paste the same goal. The build picks up from `docs/build/STATUS.md`.

## Watching progress and token use

- `/goal` with no text, in the session: elapsed time, turns, token spend, and the latest verdict on whether the goal is met.
- `docs/build/STATUS.md`: each module or issue, its state and its verification result.
- `python scripts/usage_report.py`, in a second terminal at any time once the skeleton exists: tokens by model, agent, module, sub-project and day, with estimated cost. It also writes `docs/build/usage.html`, which you can open in a browser.
- After goal 1, the factory's dashboard: `cd factory; python -m http.server`, then `http://localhost:8000/dashboard.html`.
- `/goal clear` stops the build.

## Goal 1: sub-project 1, pushed, factory installed

```
/goal This condition is met by EITHER outcome A OR outcome B; either one alone is enough, and once either is shown the session may stop. OUTCOME A, HANDOFF (this session's share is finished; the build itself need not be): the session's last turn shows the output of `python scripts/handoff.py verify` beginning with HANDOFF VERIFIED, which proves the handoff record is committed and pushed on a clean tree, and the final message begins with the line HANDOFF COMPLETE. When outcome A is shown, the condition is met; do not require any build check. OUTCOME B, BUILD DONE: Sub-project 1 of docs/superpowers/specs/2026-11-13-seth-unified-platform-design.md is built and pushed, and the software factory is installed, following docs/build/CHARTER.md. Read both in full first; the charter is your standing instruction and the owner is not available. Done means that in one final turn you ran each check below and the output shown in that turn proves it: (1) from a clean checkout of main, the secret scan, ruff format --check, ruff check, mypy strict, lint-imports, the full backend test suite, and the front-end lint, typecheck, tests and build all exit 0, with the backend suite's wall-clock time printed; (2) the row-level security schema test and every cross-client test pass, and the test that the application refuses to start under a superuser, table-owner or BYPASSRLS database role passes; (3) the schema comparison against SETH at commit 3ba0e06 passes for every ported module; (4) docs/build/STATUS.md is printed and every SETH module in the design's layer order is marked done, or blocked with evidence, with none left in progress, and every major audit finding left open is listed there as a known defect; (5) `python scripts/usage_report.py` exits 0 and prints token totals by model, by agent type and by module; (6) `git status` is clean and `git rev-parse main` equals `git rev-parse origin/main`; (7) the factory is installed as charter section 6 describes: `bash factory/checks.sh` exits 0, `factory/bin/job queue` runs, and the install commit is on origin/main. Constraints that must hold throughout: nothing in the three source repositories is changed; no test, type check, lint rule or import contract is removed or loosened to make a check pass; nothing is force-pushed; no secret is committed and the contents of .env.local are never printed. A step that depends only on an owner input left blank in charter section 2 counts as satisfied when STATUS.md lists it as waiting on owner input. If the design's Status line does not say approved, stop and report that; do not build.
```

## Goal 2: sub-projects 2 to 8 through the factory

Start this in a new session after goal 1 is met.

```
/goal This condition is met by EITHER outcome A OR outcome B; either one alone is enough, and once either is shown the session may stop. OUTCOME A, HANDOFF (this session's share is finished; the build itself need not be): the session's last turn shows the output of `python scripts/handoff.py verify` beginning with HANDOFF VERIFIED, which proves the handoff record is committed and pushed on a clean tree, and the final message begins with the line HANDOFF COMPLETE. When outcome A is shown, the condition is met; do not require any build check. OUTCOME B, BUILD DONE: Sub-projects 2 to 8 of docs/superpowers/specs/2026-11-13-seth-unified-platform-design.md are built through the installed software factory, following section 7 of docs/build/CHARTER.md. Read the design, the charter and docs/build/STATUS.md in full first; the charter is your standing instruction and the owner is not available. Done means that in one final turn you ran each check below and the output shown in that turn proves it: (1) `gh issue list --label factory --state open` returns nothing and `factory/bin/job queue` shows no unfinished job; (2) `gh pr list --state open` returns nothing; (3) from a clean checkout of main, the secret scan, ruff format --check, ruff check, mypy strict, lint-imports, the full backend test suite, and the front-end lint, typecheck, tests and build all exit 0; (4) the row-level security schema test and every cross-client test pass, including for every module added since sub-project 1; (5) docs/build/STATUS.md is printed and, for each of sub-projects 2 to 8, names its design file and records its "done when" result from the design as one of: met; met against the fake platform with live validation waiting on owner input; or blocked with evidence; and every major audit finding is shown as closed or as waiting on the owner's written acceptance; (6) `python scripts/usage_report.py` exits 0 and prints token totals by model, by agent type and by sub-project; (7) `git status` is clean and `git rev-parse main` equals `git rev-parse origin/main`. Constraints that must hold throughout: nothing in the three source repositories is changed; no change that removes or weakens a cross-client test, the row-level security schema test or the startup check on the database role is merged; no test, type check, lint rule or import contract is removed or loosened; only test organizations are used, never real client data; nothing is deployed to or written to any platform other than a development target named in charter section 2; no integration is marked validated unless it ran against a real platform during this build; nothing is force-pushed; no secret is committed.
```

## Goal 1, one unit (parallel mode)

`scripts/supervisor/supervise.py 1 --parallel 3` fills in the `<<...>>` fields from `docs/build/units.json` and starts one lead per unit in that unit's worktree. Not for pasting by hand.

```
/goal This condition is met by EITHER outcome A OR outcome B; either one alone is enough, and once either is shown the session may stop. OUTCOME A, HANDOFF (this session's share is finished; the build itself need not be): the session's last turn shows the output of `python scripts/handoff.py verify` beginning with HANDOFF VERIFIED, which proves the handoff record is committed and pushed on a clean tree, and the final message begins with the line HANDOFF COMPLETE. When outcome A is shown, the condition is met; do not require any build check. OUTCOME B, UNIT MERGED: unit <<id>> of goal 1 (<<title>>) is built and merged to main, following docs/build/CHARTER.md, sections 10 and 11 in particular. Read the charter, docs/build/STATUS.md, your entry in docs/build/units.json and docs/build/HANDOFF.md first; the charter is your standing instruction and the owner is not available. Other leads build other units in parallel: you work only in this checkout, <<worktree>>, on branch <<branch>>, and you change shared files only under the locks section 11 describes. Done means that in one final turn the output shown proves all of: (1) `python scripts/gates.py --ref <sha> --name <<id>>-merge` reported all 13 gates passing for the exact commit, rebased onto origin/main, that was then pushed to main; (2) `python scripts/unit.py done <<id>>` printed UNIT DONE; (3) `git status` is clean and the merge lock is released. Constraints that must hold throughout: nothing in the three source repositories is changed; no test, type check, lint rule or import contract is removed or loosened to make a check pass; nothing is force-pushed; no secret is committed and the contents of .env.local are never printed.
```
