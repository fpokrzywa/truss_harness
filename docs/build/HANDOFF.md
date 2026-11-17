# Handoff — 2026-11-17, after unit `suite-speed-2` (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Older handoff: `af7dbb8:docs/build/HANDOFF.md`.

**State:** no agent and no gate run is running; no merge is half done; no lock is held (`python scripts/unit.py locks` is empty); `main` = `origin/main`.

## What was just finished

- Unit `suite-speed-2` (B-2510) merged to main `69be4b9` after 13/13 gates (`suite-speed-2-merge2`): full backend suite 3183 passed in 1149.2 s, under the 1200 s target. Tests-only changes; nothing removed, skipped or loosened. It is not in `units.json` (that file lists goal-1 units; `scripts/tests/test_parallel.py` pins `goal1-final` last).
- Charter 7 started:
  - Labels `factory`, `factory:needs-info`, `factory:needs-human`, `known-defect`, `sp2` created.
  - 18 known-defect issues filed, #1-#18 (one per open row of STATUS "Known defects").
  - Sub-project 2 short design `docs/superpowers/specs/2026-11-17-subproject-2-native-delivery-databricks-design.md`: drafted by an opus agent, read-checked by a fresh sonnet builder (30 guesses), all settled in its section 9 (B-2500, B-2501). Committed `3292ab3`.
  - Sub-project 2 cut into 10 issues #19-#28 (titles `SP2-1`..`SP2-10`, labels `factory`, `sp2`), in dependency order; (C) ones are isolation-critical.

## Exact next step

1. `factory/bin/job queue` to see the queue (28 issues), then `/factory run`. Suggested order: SP2 issues #19-#28 first (they chain: 1, 2, 3, 4, 5, 6, 7; FE 8 after 3; 9 after 6 and 8; 10 last), with known-defect issues between them as capacity allows; all known defects must be closed or accepted by the end of sub-project 4.
2. For every PR the factory escalates (all migrations and all (C) issues will be escalated by the approver, B-2481): run the full suite on it (`python scripts/gates.py --ref <sha> --name <job>`) and `isolation-reviewer`, then merge or send back (charter 7.5).
3. After SP2: record its done-when result in STATUS (expected: met against the fake platform; live validation waiting on owner input, no `.devdata/databricks.env`), then sub-project 3's short design (Entra waits on owner input: no `.devdata/entra.env`).

## Things learned

- One factory job (intake, spec, build, gate, reviews, approve) will likely not fit in a lead context already past ~140k; start `/factory run` in a fresh session.
- Heredocs containing apostrophes inside a Bash tool command can fail to parse; write helper scripts with the Write tool to `%TEMP%` instead.
- `lint-imports` can crash with a cp1252 UnicodeEncodeError when its output is redirected; `PYTHONIOENCODING=utf-8` avoids it (gates.py is unaffected).
- A rebased unit branch cannot be pushed to its own remote branch without force; per charter 11 it reaches origin only by fast-forwarding main (origin `suite-speed-2` keeps the pre-rebase commit `d3c9819`).

## Worktrees

`.worktrees/suite-speed-2` (merged), `.worktrees/suite-speed-2-merge` (gate checkout) and all older worktrees listed by `git worktree list` are merged and can be removed.
