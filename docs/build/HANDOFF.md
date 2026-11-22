# Handoff — 2026-11-22 ~08:30 UTC, charter 7 factory run (lead session 9cf5f6f9)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 2 (S-021). No agent and no gate of this session is running at handoff. Lead context reached ~180k (limit 200k).

## Finished this session
- Merged: #69 job 060 (PR #146, hand merge `87208f3`, owner approval S-023); #71 job 061 (PR #149, `job merge`); #72 job 062 (PR #150, hand merge `28deb39` after a DECISIONS conflict); #91 job 064 (PR #152, `job merge`, `6bc4a6a`). Evidence in B-2734, B-2735.
- Filed #148 (final-deployment immutability in the status-guard trigger; #72 point 2; needs a lead-written migration, hand merge under B-2705 with isolation review).
- Decision ids: B-2736 (job 062), B-2737 (job 063, on its branch), B-2738 (job 064) are used. Next free for the lead is **B-2739**.

## Jobs and their stages (first action: `PYTHONUTF8=1 factory/bin/job queue`)
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 063-sp2-7-follow-up | #74 | #151 | needs-human | **Owner approval S-024 already given** (on main). Sync the branch with main (DECISIONS append conflict likely: keep both rows), `gates.py --name 063-merge` 13/13 in an agent (a failure outside the diff that passes alone on rerun does not block, S-023), hand merge `gh pr merge 151 --merge --match-head-commit <sha>`, record B-2739. |
| (new) | #93 and onward | - | queue | `factory/bin/job create <n>` for each `new` line, start at intake. |

Then the `hold` issues in queue order: #108, #109, #115, #123-#135, #139-#142, #144, #147, #148. SP4: #126 (protected migration, hand merge under B-2705 with isolation review) before #127/#128; #141 and #148 need a lead-written migration.

## Traps learned this session
- Two gates at once (or a gate plus a builder's test run) overload the cluster: `checks.sh` then fails with psycopg `ConnectionTimeout` in tests outside the diff (063 round 1; 062 post-sync). Chain gates one after another (`until grep -q '^GATE:' <other log>; do sleep 20; done; factory/bin/gate ...`).
- `job queue` hung ~10 min dropping a merged job's database while a gate ran (known checkpoint stall; it drains).
- The lead shell has `VIRTUAL_ENV` set to main's `backend/.venv`; `unset VIRTUAL_ENV` before `factory/bin/gate` and `job merge`, or demos/tests load main's code (builder 063 found this).
- `job merge` stops at `needs-human` on a DECISIONS.md append conflict; resolve in the job worktree (keep both rows in id order), push, run `bash factory/checks.sh` there, then hand merge.
- A builder's "Agent stalled" failure notice may arrive after its READY hand-back; the work is already pushed, so check `build.md` before redoing anything.
