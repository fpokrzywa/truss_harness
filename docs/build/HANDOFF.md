# Handoff — 2026-11-18 04:35, charter 7 factory run (lead, session after 17267497)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress"), then this file. Older handoff: `f69f847:docs/build/HANDOFF.md`.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` (plus this handoff commit).

## What was just finished

- Merged PR #42 (job 009, #6 F-09 OAuth2 REST adapter, `2003a3a`): round 2 code + security PASS, isolation review 2 APPROVED (`reviews/f09-isolation-review-2.md`), approver escalated (adapter outbound change), lead merged main into the branch and merged after `gates.py --name 009-merge` 13/13 (3505 passed, 1659 s).
- Merged PR #43 (job 010, #8 ARCH M3 wedged worker, `93e3434`): gate PASS, code PASS, isolation review 1 APPROVED (`reviews/arch-m3-isolation-review-1.md`), approver APPROVE; `job merge` stopped on a DECISIONS conflict (job → needs-human); lead merged main (both sides kept), `gates.py --name 010-merge` 13/13 (3506 passed, 1330 s), `gh pr merge 43`. `job queue` then recorded it `merged`.
- Filed #44 (cap abandoned run threads; isolation review note 7), label `factory`.

## Exact next step

1. `/factory run`. The queue's only `new` line is #9 (ARCH M4 / F-11 retention purge and maintenance command): `factory/bin/job create 9` → intake → route → spec → build → gate → review → approve, per the factory skill.
2. Then the queue at concurrency 1: #10..#18 (known defects), SP2-3..SP2-10 (#21-#28), #33, #34, #36, #39, #41, #44. Charter 6: concurrency may go to 2 after five issues in a row merge without a failed gate (count since the install: check `factory/bin/job summary`; jobs 009 and 010 merged with gates passing on first run in their final rounds).

## Open branches and worktrees

- Merged and safe to remove (background `git worktree remove --force`): `.worktrees/009-f-09-rest-adapter`, `.worktrees/009-merge`, `.worktrees/010-arch-m3-recover-a` (job merge's removal hit WinError 32), `.worktrees/010-merge`, `.worktrees/008-f-13-per-tool`, `.worktrees/008-merge`.

## Traps (new this session, plus the old ones that still bite)

- Reviewers (`reviewer-code`) sometimes cannot write their file and reply with its content: save it yourself to `round-N/review-<r>.md`, then `job sync`.
- `job merge` often fails on `docs/build/DECISIONS.md` conflicts because both the lead and the job append rows. Resolve in the job worktree with `git merge origin/main`, keep both sides in ascending id order, push, then run `gates.py --ref <sha> --name <id>-merge` and `gh pr merge <n> --merge`; `job queue` then records `merged`.
- For a critical module (runtime, connections, …) the lead also runs `isolation-reviewer` during review, saves it to the job folder as `isolation-review-N.md` and on the branch as `docs/build/reviews/<slug>-isolation-review-N.md`, and tells the approver to read it.
- `factory/bin/job queue` takes 1-2 minutes; give it a 300 s timeout.
- Builders sometimes leave a background `checks.sh` run after reporting READY; check for `pytest` processes before starting a gate (`Get-CimInstance Win32_Process -Filter "Name='python.exe'"`).
- Do not run two full gates at once. DECISIONS ranges: used B-2564, B-2565 (lead), B-2570 (008), B-2580, B-2581 (009), B-2590 (010). Next free: B-2566.. for the lead, B-2600.. for job 011.
- A session whose cwd is inside a job worktree blocks `job merge` from removing it (WinError 32); `cd` back to the main checkout before merging.
