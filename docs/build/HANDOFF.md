# Handoff — 2026-11-23, charter 7 factory run (lead session 09f5d0ed)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (sub-projects table), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. Context limit reached; no new agents were started after 137-140 were created.

## Finished this session
- Job 132 (#265, PR #266) hand-merged `3d44984` after `gates.py --name 132-merge` 12/13 (one connection test flake, its file 17/17 alone). Supervisor asked for an A/B check of `tests/authz/scoping/test_workspace_routes.py::test_connections_and_delivery_targets` on `3d44984` vs `3d44984^1`: result below.
- SP6, SP7, SP8 short designs written, builder-read and settled: B-3921, B-3922, B-3923. Issues: SP6 #268-#275, SP7 #277-#284, SP8 #286-#296. B-3920: job 133 records draft usage as `model_call`.
- Job 134 (#267, PR #276) hand-merged `32f667e` (B-3925): gate PASS, code+security PASS, approver ESCALATE (adapter outbound behaviour); lead accepts the stricter refusals; isolation review 1 APPROVED (`docs/build/reviews/134-isolation-review-1.md`). Follow-up #285 (SYSTEM$ / side-effecting PG functions), depends on 134.
- B-3924: A/B evidence for the 132 hand merge (supervisor). **Next free lead id: B-3926.**

## Open jobs (see `factory/bin/job queue`)
- **133-sp5-2-appgen-skeleton** (#255, PR #298): stage `review` round 1, gate PASS. Spawn reviewers code+security (opus). Has migration `appgen_0001` (protected): expect ESCALATE -> lead isolation review + `gates.py` + hand merge. Builder call to settle: `discovery_id` guarded by a SECURITY INVOKER trigger instead of a composite FK (ported table lacks `(tenant_id, id)` unique key); `shifde_app` UPDATE on drafts is 42501 (no grant).
- **135-sp6-2-adapters-native** (#269, PR #297): stage `review` round 1, gate PASS. Spawn reviewers code+security. Vendor adapter (critical): expect ESCALATE -> isolation review + `gates.py` + hand merge.
- **136-sp7-5-board-read** (#281, PR #299): stage `review` round 1, gate PASS. Spawn reviewers code+security. Lead accepted the additive `tests/app/test_routes.py` BOARD set (AC8 literal reading would break the suite) and the new "board writes nothing" contract with `allow_indirect_imports`.
- **137-sp8-2-shifde-mcp** (#287), **138-sp8-4-adapters-native** (#289), **139-sp8-6-adapters-native** (#291), **140-sp8-8-adapters-native** (#293): created, stage `intake`; spawn intake for each.
- New and now unblocked: **#268** SP6-1 (job 134 merged), **#285** (job 134 merged). #256/#257 unblock when 133 merges; #270 when 135 merges; #282 when 136 merges.

## Dependency rules for `new` lines (start only when deps merged)
- SP5: #256 and #257 after #255 (job 133); then per each issue body. SP5-4 (#257) needs the lead to add appgen-web lines to protected `factory/checks.sh`.
- SP6: #268 after #267 (job 134, moves sqlrules to core); #270 after #269 (job 135); #271..#275 wait on #262 (SP5-9) as their bodies say.
- SP7: #277 after #255; #278 after #277; #282 after #281 (job 136); others per body.
- SP8: #286 after #270+#271; #288 after #287+#279-#281; #290/#292/#294 per body.
- #285 after #267.
- Migrations (`backend/migrations/versions/`) are protected: those jobs escalate; the lead runs isolation-reviewer + `gates.py` and hand-merges (`gh pr merge --match-head-commit`).

## Lead work still open
- #174 items 1, 3, 4; #147 part 2; B-3081/B-3905 schema-test move. Stale STATUS known-defect rows: REQ F-03, Item 20, Item 21.

## Traps learned
- Any lead commit to `DECISIONS.md` on main makes every open job branch conflict at `job merge`: resolve in the job worktree (keep both sides, main's first), push, and hand-merge if the new diff is docs only.
- `cd` out of a job worktree before `job queue` (WinError 32 on remove).
- Earlier traps hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV).
