# Handoff — 2026-11-17 evening, charter 7 factory run (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md` (see "Charter 7 progress"), then this file. Older handoff: `69be4b9:docs/build/HANDOFF.md`.

**State:** no agent and no gate run is running; no merge is half done. `main` = `origin/main` (plus this handoff commit).

## What was just finished

- Merged: PR #32 (job 006, #7 J-M4 `/api/ready` 3 s deadline, B-2523) and PR #31 (job 004, #3 F-12 capture off, `b0baaaa`; four `deliberate_changes.yaml` entries added by the lead after isolation review, full gates 12/13 with the one failure a load 502 that passed on rerun, B-2530).
- New lead ruling B-2530: protected schema declaration entries (yaml, definer allow-list) are added by the lead on the PR branch after isolation review, then full gates, then `gh pr merge --match-head-commit`. Never `test_rls_schema.py`.
- Follow-up issues filed: #33, #34, #36 (B-2532). Binding criterion added on #21 (workspace-alias check).

## Exact next step

1. `/factory run`. `factory/bin/job queue` shows `resume 20 005-sp2-2-delivery-targets build round 4`: spawn a new `builder` (name `builder-005-sp2-2-delivery-targets`, port 5150) told it is resuming, "The user gave feedback: read feedback-3.md ...". It updates the head-pin test `tests/identity/test_schema.py::test_a_head_named_in_depends_on_is_not_a_version_head` (delivery_0001 is now the head, not tenancy_0004). It will likely end BLOCKED or READY; either way the lead then: merge origin/main into the branch if main moved, `python scripts/gates.py --ref <sha> --name gate-005`, and if 13/13 (rerun any load-only timeouts in isolation), `gh pr ready 35; gh pr merge 35 --merge --match-head-commit <sha>`. Isolation review of PR #35 is already APPROVED (port fix and two tests done in round 3); only re-review if round 4 touches more than tests.
2. Then job 001 (#1 F-03, PR #29, head `5fef51e`, parked `needs-human`): isolation re-check APPROVED. Merge origin/main into its worktree branch (expect conflicts in DECISIONS.md and in `tests/identity/test_schema.py` head pins: after both, heads include `tenancy_0005`, `delivery_0001`, `runtime_0003`), push, full gates, merge by hand. If the merge needs code changes, send them through `factory/bin/job feedback 1` instead of editing.
3. Then job 002 (#2 J-M2, `needs-human`): give feedback that it may add a new tenancy migration (next after `tenancy_0005`) granting `UPDATE (settings)` on `tenants` to `shifde_app`, and stop BLOCKED with the `deliberate_changes.yaml` entry (B-2530 flow).
4. Then the queue at concurrency 1: #4 onward, SP2-3..SP2-10 (#21-#28), #33, #34, #36.

## Open branches and worktrees

- `.worktrees/005-sp2-2-delivery-targets` on `factory/005-sp2-2-delivery-targets` @ `56dec89` (pushed, clean), PR #35 draft.
- `.worktrees/001-f-03-suspend-a` on `factory/001-f-03-suspend-a` @ `5fef51e` (pushed, clean), PR #29.
- `.worktrees/002-j-m2-supported-setter` (job 002, WIP `2e73a90`, no PR).
- `.worktrees/gate-004`, `.worktrees/gate-005`, possibly `.worktrees/gate-001` (stopped run): gate checkouts, safe to remove with `git worktree remove`.
- Merged job worktrees `003-*`, `004-*`, `006-*` can be removed.

## Traps

- Two jobs can pick the same next migration number; the second to merge renumbers (B-2532).
- Builders' permission classifier refuses even temporary local edits to protected files; they report the entry instead. The lead's own edits worked.
- Full suite under load: 502 upstream timeouts and `test_p8_perf` timing fail; rerun the failing file in the gate worktree before judging.
- Writing files with Windows Python from bash: use repo paths, not `/tmp` (different roots).
- DECISIONS numbers: job branches pick numbers independently; check for clashes at merge (main has B-2523 = `/api/ready`, B-2530..B-2532 = lead; branch 005 uses B-2524..B-2526, branch 001 B-2511/B-2512, F-12 B-2514).
