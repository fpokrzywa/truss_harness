# Handoff — 2026-11-23 ~17:10 EST, charter 7 factory run (lead session a76bd9e5)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (sub-projects table), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. Lead context ~190k at handoff. No agent running.

## Finished this session
- Merged 139 SP8-6 (#291, PR #302, 13/13 at e2410d5) and 141 SP6-1 (#268, PR #307, 13/13 at 0d48f8f).
- Added the appgen-web lines to protected `factory/checks.sh` (B-3951, commit c35f37f), then started #257.
- New jobs: 148 (#257 SP5-4 appgen-web), 149 (#277 SP7-1 internal tools), 150 (#318 home page redirect, owner request via supervisor ruling S-038 — reply to the supervisor session when it merges).
- Follow-ups filed (label factory): #315 (deadlock on secret `last_used_at` under concurrent evaluation case runs), #316 (SP5-3 accept retry robustness / screens visibility / failed-step audit; depends on #256), #319 (Snowflake `:<digit>` refusal; depends on #268).
- **Next free lead decision id: B-3952.**

## Exact next step: gate and merge, strictly one gate run at a time
All branches below are synced with origin/main as of ~17:05 and pushed, clean worktrees. For each, in this order: if `git rev-list --count origin/factory/<branch>..origin/main` > 0, sync again (merge origin/main, keep both, main first; alembic-heads lists include every head); then `python scripts/gates.py --ref <full sha> --name <id>-merge > .worktrees/_gate-logs/<id>-merge.out 2>&1`, read the **final** `gates: N/M passed` line (the first `gates: ref ...` line is NOT the result — that trap caused an overlapping run this session), then `gh pr merge <pr> --merge --match-head-commit <sha>`, then `PYTHONUTF8=1 factory/bin/job queue`.
1. **137** SP8-2 shifde-mcp (#287, PR #310), head c742c99. First run at 61088d5 was 12/13 (overlapped with a factory gate); a sync agent was rerunning at c742c99 when this lead stopped — check `.worktrees/_gate-logs/137-merge.out`: if it ends with `gates: 13/13 passed` at c742c99, merge; else rerun. File follow-ups: limiter order, docstring, removed-member key test.
2. **147** SP5-3 appgen accept (#256, PR #314), head 64f0c82, needs-human only because `job merge` hit a conflict (now resolved and pushed). Approver APPROVE, reviews PASS. Run 13 gates, then merge.
3. **148** SP5-4 appgen-web (#257, PR #313), head 2154caa, needs-human (approver escalated for the `scripts/gates.py` change; lead reviewed: additive only, lockfile drop of optional `@babel/core` peer accepted). gates.py on this branch has 18 gates; all must pass. Then merge.
4. **150** home page (#318, PR #320): stage approve, APPROVE. Run `PYTHONUTF8=1 factory/bin/job merge 150-home-page-send-people` (it gates and merges). Tell the supervisor session the result.
5. **149** SP7-1 internal tools (#277, PR #317): stage approve, APPROVE. `job merge 149-sp7-1-runtime-internal`.
6. Then the remaining needs-human: 145 (PR #311; add notes to B-3934/B-3936 that B-3947 supersedes them), 140 (PR #303), 143 (PR #308) — sync, 13 gates, merge, as before.
Use a general-purpose sonnet agent per merge (sync, touched tests, push, gates.py, report gate lines); never two gate runs at once (no `factory/bin/gate` during a `gates.py` run).

## Open jobs (stage)
137 needs-human · 140 needs-human · 143 needs-human · 145 needs-human · 147 needs-human · 148 needs-human · 149 approve · 150 approve.
Startable after merges: #258 (needs #256+#257), #260 (needs #256+#257), #278 (needs #277), #316 (needs #256), #319 (#268 merged — startable), #315 (startable), #312 (needs #285). Others per dependency rules in the issues and previous handoffs. `job queue` does not know dependencies; check "Depends on" in the issue body before `job create`.

## Traps learned
- Short job ids resolve to issue numbers (`route 148` hit issue #148). Always pass the full job id.
- Reviewers sometimes don't write their file (reviewer-ui did not); the lead writes it verbatim from the report, marked as such.
- `agent-browser` is not installed: UX/UI reviews are diff-and-test only.
- Earlier traps hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, `gates.py` writes no `.summary`, builders may stall after reporting READY — check build.md first line).
