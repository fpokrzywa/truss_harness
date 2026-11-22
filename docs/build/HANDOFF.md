# Handoff — 2026-11-21 ~23:20 UTC, charter 7 factory run (lead session 8b55f00e)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. No agent of this session is running. Cluster on 55450 running. `concurrency` 2 (S-021). At handoff a `factory/bin/gate --hook` process (started 18:17 local by the factory's own hook, not by the lead) was running checks in the 053 worktree; check `factory/jobs/053-arch-m11-rest-data/round-2/checks.md` before rerunning that gate.

## Finished this session
- Merged #58 job 054 (PR #118, `job merge`).
- 050: S-020 feedback round 2 done; all reviews PASS; isolation review 2 APPROVED (`docs/build/reviews/050-isolation-review-2.md`); branch synced with main at `b812b9e` (B-2720 row has the round-2 addendum).
- 053: lead decisions in its feedback-1 (per-page cap accepted, follow-up #135); isolation review 1 APPROVED (`docs/build/reviews/053-isolation-review-1.md`); `053-merge` 12/13 found one real failure, fixed in round 2.
- 055: lead feedback-1: fail closed on malformed `approver_permissions` (points 2-4 accepted). Round 2 built.
- New jobs 056 (#62) and 057 (#64). Decision ids reserved: 056 -> B-2729, 057 -> B-2730. **Next free for the lead: B-2731.**

## Jobs and their stages (first action: `PYTHONUTF8=1 factory/bin/job queue`)
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 050-arch-m2-rest-move | #47 | #122 | needs-human (round 2, ESCALATE only for the `audit/`/outbound rule, as S-020 foresaw) | Under S-020: sync with main if it moved, `python scripts/gates.py --ref <full sha> --name 050-merge` (13/13; treat failures as real until shown to pass alone and be outside the diff), then `gh pr ready 122; gh pr merge 122 --merge --match-head-commit <sha>`; update STATUS row ARCH M2 and log the merge as B-2720. |
| 053-arch-m11-rest-data | #56 | #119 | gate round 2 (head `55007c0`) | `factory/bin/gate 053-arch-m11-rest-data` (unless the hook already wrote round-2/checks.md), reviewers code+security (opus), approver; it will escalate again (data export) -> lead: `gates.py --name 053-merge-r2` 13/13 then hand merge; isolation review 1 already APPROVED. Close the ARCH M11 STATUS/findings rows (both halves done). Commit `docs/build/reviews/053-isolation-review-1.md` is already on main. |
| 055-f-10-follow-up | #60 | #136 | gate round 2 (head `d69c640`) | gate, reviewers code+security (opus), approver; expect ESCALATE (access control) -> isolation review + `gates.py` 13/13 + hand merge. |
| 056-oidc-expire-the-cached | #62 | #137 | gate round 2 | gate, reviewers code+security (opus), approver. |
| 057-sp2-gateway-hardening-cross | #64 | #138 | gate round 1 | gate, reviewers code+security (opus), approver. |

Then `hold`: #66, #68, #69, #71, #72, #74, #91, #93, #108, #109, #115, #123-#135. SP4: do SP4-8 (#126, protected migration -> hand merge under B-2705 with isolation review) before SP4-4/5 (#127, #128).

## Traps learned this session
- Run gates strictly one at a time: with 3+ heavy runs, cross-client tests fail on `psycopg ConnectionTimeout` (056 round 1) and `gates.py` takes ~70 min with 6 load flakes (053). Chain them in one background shell (`until [ -s <prev output> ]; do sleep 30; done; ...`).
- `tests/app/test_routes.py` (route table vs SETH) is outside the factory core set: any new route must be added to `UNIFIED_ONLY`, and only `gates.py` catches it.
- Builders often hit the 600 s stream watchdog after reporting READY; check `head -1 build.md` and `git log origin/<branch>` before respawning.
- The isolation-reviewer is read-only: it will not write its review file; the lead saves its reply under `docs/build/reviews/`.
- `factory/bin/job feedback` refuses unless the job is in `needs-human`: advance it there first.
