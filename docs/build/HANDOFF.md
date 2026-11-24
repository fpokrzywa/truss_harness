# Handoff — 2026-11-23 ~23:40 EST, charter 7 factory run (lead session e12af1ce)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. Lead context ~185k at handoff. No agent running, no gate run.

## Finished this session
- Merged 140 (#293, PR #303), 157 (#325, PR #331), 158 (#329, PR #330), 156 (#316, PR #332), 151 (#315, PR #322). Gate counts in the STATUS entry; ruling B-3980.
- Filed #333 (flaky runtime worker test) → job 159.
- Advisories posted as issue comments: #259 (SP5-6 must gather `Resolved` under the requester's RLS context, add design 9.5 test, runbook note), #279/#280 (trusted prompt variables; bare run/job ids; proposal scoping; worker org context).
- **Next free lead decision id: B-3983.** (B-3968 is on main from 151; B-3969 on 143's branch; B-3979 on 155's branch; B-3970 on main from 157; B-3971-B-3978, B-3981-B-3982 were reserved for builders and are unused.)

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`; start every `resume`/`new` line (check "Depends on" in each issue body before `job create`; `job queue` does not know dependencies, and none of the current `new` lines 259/261/262/271-274 is startable until 153 merges).
2. **159** (#333, PR #334) at review round 1: spawn `reviewer-code` (model sonnet), then approver, then `job merge`.
3. Needs-human, each already synced with main (re-sync first: main moved): for each, merge origin/main in its worktree (DECISIONS conflicts: keep both, main first), `python scripts/gates.py --ref <sha> --name <job>-merge` (one at a time), then `gh pr merge <pr> --merge --match-head-commit <sha>`:
   - **153** (#258, PR #323): isolation APPROVED. Merging it unblocks #259 and #327 (cap body size; #327 must merge before #259 deploys the app).
   - **155** (#278, PR #328): protected migration `assist_0001`; reviews and isolation done, ruling B-3979 on the branch. Unblocks #279.
   - **143** (#285, PR #308): isolation APPROVED. Unblocks #312.
4. Startable after those merges: #327 and #259 (after 153), #279 (after 155), #312 (after 143), #324 (#315 merged: startable now).

## Open jobs (stage)
143 needs-human · 153 needs-human · 155 needs-human · 159 review.

## Traps learned
- Agents often stall ("no progress for 600s") after pushing: check `git ls-remote origin <branch>` and the worktree's `git status` before redoing anything.
- `gh pr merge` refuses on conflicts with a moved main even after a green gate run; a DECISIONS-only conflict can be resolved in the worktree (main's rows first) and checked with targeted `gates.py --tests ... --no-frontend` (B-3980).
- Full suite takes ~30 min under load; timing tests can fail when builders run `checks.sh` at the same time (#333).
- The gate's protected-path check covers modified/deleted files only; new migrations go through the approver's escalation by design.
- Reviewers sometimes report PASS without writing their file: `job advance ... approve` then refuses; resume the reviewer and ask it to write the file.
- Earlier traps hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, full job ids only, never two full gate runs at once, `gates.py` output buffered until exit).
