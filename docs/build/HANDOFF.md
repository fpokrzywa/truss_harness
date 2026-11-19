# Handoff — 2026-11-19 14:35, charter 7 factory run (lead session)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main`. Local cluster on 55450 running (started from the main checkout).

## What was just finished (this session)

- Job 023 #23 SP2-5 plans/artifacts/sealing/second-person approval: PR #67 merged by hand `1f6a273` at 12/13 (flake #51 timeout only; file passed 12/12 alone), B-2680, isolation review `reviews/sp2-5-isolation-review-1.md`; follow-ups #68, #69.
- Job 024 #24 SP2-6 deployments/unknown outcome/reconcile: PR #70 merged by hand `aca4112` at 13/13 (3788 passed), `reviews/sp2-6-isolation-review-1.md`; follow-ups #71, #72.
- Job 025 #25 SP2-7 `shifde-executor` + end-to-end against the fake: PR #73 merged by hand `7f6ade6` at 13/13 (3829 passed), B-2696 (service principal from the executor credentials file; experimental, live validation waiting on owner input), `reviews/sp2-7-isolation-review-1.md`; follow-up #74 (bind credentials entries to an organization; close before go-live).
- Job 026 #26 SP2-8 front end (Deliver nav, targets and executors screens): intake ACCEPT (medium; ui, security), routed (code sonnet, ux sonnet, ui haiku, security opus), spec written, advanced to `build` round 1. **No builder has been spawned yet.**

## Exact next step

`/factory run`: `job queue` shows 026 as `resume` at `build`. Spawn `builder` named `builder-026-sp2-8-front-end` with the header, `Build round 1. Port: 5360.`, "implement spec.md; you are the first builder (nothing built yet)". Spec assumptions accepted by the lead: Deliver screens under feature `tenancy` (no `delivery` feature flag), org-level `useCan`, first page only, disable-only executors. Then gate, the four reviewers in one message, approver; then `job queue` for #27 (SP2-9) etc.

## Pattern that worked this session (each job ~1.5-2 h)

- Isolation-critical jobs: spawn `isolation-reviewer` in parallel with the factory gate (read-only, branch diff), save its verdict to `factory/jobs/<id>/isolation-review-1.md` and `docs/build/reviews/<sp>-isolation-review-1.md`, file non-blocking notes as a `factory` issue.
- Start `gates.py --ref <head sha> --name <id>-merge` as soon as the factory gate passes (reviews run meanwhile). The approver escalates every migration/credential change; then `job advance <id> needs-human`, and after 13/13 `gh pr merge <pr> --merge --match-head-commit <sha>`; `job queue` marks it merged.
- Flake #51 (`test_p6_exec2.py::test_write_node_failure..._never_retried` timeout) still occurs under full-suite load; precedent B-2631/B-2680: rerun that file alone in the job worktree, merge if it passes and the branch does not touch the runtime executor.

## Open branches and worktrees

- `.worktrees/026-sp2-8-front-end` on `factory/026-sp2-8-front-end` (spec only, no commits beyond base).
- Merged/detached: `.worktrees/023-*`, `024-*`, `025-*` job worktrees and `*-merge` gate checkouts, safe to remove.

## Traps

- **Never `cd` into a worktree in a Bash call** except as `cd <wt> && ... ; cd <main>`; prefer `git -C`.
- `factory/bin/gate` needs the full job id. `factory/bin/job queue` takes 1-2 min.
- Edit DECISIONS/STATUS with Python `newline=''`. DECISIONS next free id: **B-2697**.
- Do not run two full gates at once (~40 min each).
- Every new route: classify in `backend/tests/authz/scoping/test_workspace_routes.py` (checks.sh does not run it); say so in the spec-writer and builder prompts.
- SECURITY DEFINER: B-2530 flow (builder records allow-list entry; lead commits it to `backend/tests/schema/test_function_schema.py`; merge by hand).
- Builders must not restart the cluster (B-2632).
