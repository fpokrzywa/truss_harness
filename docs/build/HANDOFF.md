# Handoff — 2026-11-19 08:30, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main`. Local cluster on 55450 running (started from the main checkout). The supervisor's salvage stash of session e2887bac was inspected and dropped (B-2674); no worktree held uncommitted work.

## What was just finished

- Job 022 (#22 SP2-4 delivery job queue, executor gateway, journal; PR #65, isolation-critical) merged by hand `4466c79` under B-2530: isolation reviews 1-2 APPROVED (`docs/build/reviews/sp2-4-isolation-review-1..2.md`, salvaged and committed), allow-list entry B-2671 committed by the lead (`05d3a01`), `gates.py --name 022-merge` 12/13 (six `/api/executor/v1` operations unclassified in `tests/authz/scoping/test_workspace_routes.py`), feedback-2 round 3 (`a9589cb`, `a6b8c55`), `gates.py --name 022-merge-r3` 13/13 (3721 passed in 1682 s). `job queue` shows it `merged`. Follow-up #66 (isolation review 2 findings 2-3).

## Exact next step

`/factory run`. `factory/bin/job queue` says: `new 23` (SP2-5: artifacts, plans, sealing, second-person approval, isolation-critical), then the held list: #24-#28 (SP2-6..10), #33, #34, #36, #39, #41, #44, #46, #47, #51, #55, #56, #58, #60, #62, #64, #66.
- SP2-5/SP2-6 are bound by B-2673: composite `(tenant_id, plan_id)` and `(tenant_id, deployment_id)` FKs on `delivery_jobs` plus a cross-client test where B's job names A's plan or deployment id. Put this in the answer/spec if intake or spec-writer misses it.
- Any job that adds a new route must classify it in `backend/tests/authz/scoping/test_workspace_routes.py` (F-08 route test); `checks.sh` does not run that test, only the full suite does. Tell builders up front (it cost job 022 a round).
- Any job adding a SECURITY DEFINER: B-2530 flow (builder records the verbatim allow-list entry in DECISIONS; after isolation review the lead commits it to `backend/tests/schema/test_function_schema.py` on the branch; the factory gate then always FAILs with "protected paths changed" and `job advance <id> needs-human` is refused from `gate`, which is fine: run `gates.py --ref <sha> --name <id>-merge`, then `gh pr merge <pr> --merge --match-head-commit <sha>`; `job queue` marks it merged).

## Open branches and worktrees

- None live. `.worktrees/022-sp2-4-delivery-job` (job branch, merged) and every `*-merge*`, `verify*`, `goal1-*` worktree are detached or merged checkouts, safe to remove (`job queue` normally cleans the job one).

## Traps (new this session, plus older ones)

- **Never `cd` into a worktree in a Bash call**, even inside a compound command: the session's cwd follows it. Use `git -C`, absolute paths, or `cd <main checkout> && ...`.
- `factory/bin/gate` needs the full job id (`022-sp2-4-delivery-job`), not the issue number (`job` accepts both, `gate` does not).
- Editing DECISIONS/STATUS with Python on Windows: open with `newline=''`, or the file is written CRLF (git normalises it, but the working copy shows noise).
- A lead DECISIONS row written while a job branch also appends rows: commit locally, merge the PR first, then `git pull --rebase` and keep both sides of the conflict.
- Escalations for CI config or new dependencies are routine: as the human run `gates.py` (13/13) plus an `isolation-reviewer`, save the review as `isolation-review-1.md` in the job folder, then `gh pr merge --match-head-commit`.
- A builder that restarts the cluster from its worktree pins that folder; tell builders not to restart the cluster (B-2632).
- Reviewers sometimes cannot write their file and reply with its content: save it to `round-N/review-<r>.md`, then `job sync`.
- `factory/bin/job queue` takes 1-2 minutes (300 s timeout). Do not run two full gates at once (each ~38 min now).
- DECISIONS next free id: B-2675.
