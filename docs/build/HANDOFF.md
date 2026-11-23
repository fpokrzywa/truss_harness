# Handoff — 2026-11-23 12:50 EST, charter 7 factory run (lead session cdcd7d36)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (sub-projects table), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. Lead context reached 200k; no agent is running at handoff.

## Finished this session
- Merged: 138 SP8-4 Fabric native adapter (#289, PR #304) via `factory/bin/job merge`.
- Lead isolation reviews (records in `docs/build/reviews/`): 146 APPROVED, 141 APPROVED, 137 APPROVED, 140 BLOCKED (sent back as feedback-1; round 3 built). Decisions B-3944..B-3946. **Next free lead id: B-3947.** Job branches still carry B-3926 (143 and 146 both): renumber the later one at merge (keep both, main first).

## Open jobs (`PYTHONUTF8=1 factory/bin/job queue`) and exact next steps
- **139-sp8-6-adapters-native** (#291, PR #302): needs-human, isolation APPROVED. Synced with main at `c36295a` (pushed). `python scripts/gates.py --ref c36295aa6a0dc358e49524f9b4b6b3b2bfcc15a8 --name 139-merge` was running at handoff (in the full suite); read `.worktrees/_gate-logs/139-merge.summary`. If 13/13: main has since gained 138 (PR #304), so re-sync, rerun tests of changed modules, then hand merge (`gh pr merge 302 --merge --match-head-commit <sha>`). If the summary is missing, the run died with the session: rerun after a re-sync.
- **133-sp5-2-appgen-skeleton** (#255, PR #298): needs-human after round 4 (feedback-2 fixed the version-head test; code+security PASS; isolation APPROVED earlier). Open product call decided: B-3944 (`model_call` usage type). Next: sync with main, `gates.py --name 133-merge-r2`, hand merge. Then #256, #257 may start (#257 needs appgen-web lines in protected `factory/checks.sh`).
- **146-sp6-2-follow-up** (#300, PR #309): needs-human, approver ESCALATE (vendor adapter); isolation APPROVED (`reviews/146-isolation-review-1.md`). Next: renumber its B-3926 row, sync, `gates.py`, hand merge. Must merge before 145.
- **141-sp6-1-sql-tools** (#268, PR #307): needs-human, approver ESCALATE (vendor adapter outbound); isolation APPROVED (`reviews/141-isolation-review-1.md`). Next: sync, `gates.py`, hand merge. File follow-up: refuse `:<digit>` in the Snowflake reading.
- **137-sp8-2-shifde-mcp** (#287, PR #310): needs-human (migration `mcp_0001_access`, key login); round 2 all four reviews PASS; isolation APPROVED (`reviews/137-isolation-review-1.md`). Next: after 133 merges, re-edit `tests/identity/test_schema.py` version heads (both `appgen_0001` and `mcp_0001` heads, without weakening) via `job feedback 287`, or if 137 goes first then 133 needs it. Then `gates.py`, hand merge. File follow-ups from the review (limiter order, docstring, removed-member key test).
- **140-sp8-8-adapters-native** (#293, PR #303): stage **gate**, round 3 (feedback-1 isolation fixes built at `3936822`). Next: `factory/bin/gate 140-sp8-8-adapters-native`, then reviews (code, security), then approver (expect ESCALATE), then a second lead isolation review of the feedback-1 fixes, `gates.py`, hand merge.
- **143-read-only-sql-check** (#285, PR #308): stage **gate**, round 3 (U&/UESCAPE refused at `ceb9d43`). Next: `factory/bin/gate 143-read-only-sql-check`, then reviews (code, security). File the out-of-scope follow-up (Snowflake `IDENTIFIER('SYSTEM$...')`, Postgres `query_to_xml('...')`).
- **145-sp6-3-delivery-snowflake** (#270, PR #311): stage **build**, round 2, no builder running. Wait until 146 merges, then spawn the builder with: round-1 reviews in `round-1/` (code CHANGES: `tests/identity/test_schema.py` must now expect `delivery_0007` head and `delivery_0006` not a head, plus docstring); security PASS with a hold: switch `delivery.service.claim_key` to #300's `address_key(snowflake_claim_identity(address))` and `executor/credentials.py snowflake_account()`, refuse a target whose egress host does not reduce to the claimed identity, add a cross-org privatelink/locator 409 test (see `reviews/146-isolation-review-1.md` follow-ups 1 and 2). Merge 145 after 146.
- New lines in the queue: #256, #257, #258 (SP5) wait on 133; do not create them before 133 merges. Other `hold` lines per the dependency rules below.

## Dependency rules for `new` lines
- SP5: #256/#257 after #255 (job 133). SP5-4 (#257) needs the lead to add appgen-web lines to protected `factory/checks.sh`.
- SP6: #271..#275 wait on #262 (SP5-9). SP7: #277 after #255; #278 after #277; others per body. SP8: #286 after #270+#271; #288 after #287+#279-#281; #290/#292/#294 per body (and carry the preconditions commented on them).

## Traps learned
- `factory/bin/job queue`, `feedback` and other job commands block while `factory/bin/job merge` holds a job lock (it runs the gate): run them in the background or wait.
- Builders often stall at the end ("no progress for 600s") after pushing: check `build.md` first line and the branch before respawning (happened to 133 and 143 this session; both were READY).
- Isolation reviewers will not write files; save their reply yourself under `docs/build/reviews/`.
- `gates.py` full suites in parallel cause flakes; run one at a time (~36 min each).
- Earlier traps hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, DECISIONS conflicts on job branches, a `cd` into a worktree moves the session cwd).
