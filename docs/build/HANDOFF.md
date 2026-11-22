# Handoff — 2026-11-22 ~00:15 UTC, charter 7 factory run (lead session 5018289e)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 2 (S-021). No agent of this session is running at handoff. A `scripts/gates.py --ref fb38b0e5189e9978e8bb789c48cfe9a1cbd3ddf6 --name 050-merge-r3` process started 19:03 local by this lead finished 12/13 (see the 050 row): its summary is in `.worktrees/_gate-logs/lead-050-merge-r3.out` (logs in `.worktrees/_gate-logs/050-merge-r3/`). No gate is running.

## Finished this session
- Merged #60 job 055 (PR #136, `job merge`, `1618133`). The `job merge` worktree removal failed (folder in use) after the merge; rerunning `job merge` showed the job already `merged`. The leftover `.worktrees/055-f-10-follow-up` can be removed.
- 053: round 2 gate PASS, code+security PASS, approver ESCALATE (data export) -> needs-human.
- 056: round 2 gate PASS, code+security PASS, approver ESCALATE -> needs-human. Lead calls: accept the 3600 s default and cap and failing closed when the JWKS endpoint is down (design: deny on unknown). Isolation review 1 APPROVED (`docs/build/reviews/056-isolation-review-1.md`). Follow-ups #139 (over-long max-age), #140 (rate-limit test endpoint refetch).
- 057: round 1 gate PASS, code+security PASS, approver ESCALATE (audit/, executor auth) -> needs-human. Lead accepts the NULL-tenant `delivery.executor.token.rejected` row, the commit in the auth dependency and the 429-before-lookup behaviour (isolation review confirms all three). Isolation review 1 APPROVED (`docs/build/reviews/057-isolation-review-1.md`). Follow-up #142.
- 050: branch synced with main at `fb38b0e` (DECISIONS conflict, kept B-2720 and B-2728). `gates.py --name 050-merge-r3` started.
- New job 058 (#66): intake ACCEPT rescoped (item 1 already on main, item 3 design doc alignment); item 2 (lease cap, needs a protected migration) split out to #141 for the lead. Spec written; builder round 1 READY, draft PR #143, job at gate.
- Decision ids: next free for the lead is still **B-2731** (none used this session).

## Jobs and their stages (first action: `PYTHONUTF8=1 factory/bin/job queue`)
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 050-arch-m2-rest-move | #47 | #122 | build round 3 (lead feedback-2) | `gates.py --name 050-merge-r3` on `fb38b0e`: 12/13, 4171 passed, 1 real failure of the diff: `tests/app/test_routes.py::test_the_mounted_route_table_is_sethss_minus_branding` (its two new GET `.../discovery-jobs/{job_id}` and `.../health-check-jobs/{job_id}` routes are not in `UNIFIED_ONLY`). feedback-2.md filed. Next: spawn `builder-050-arch-m2-rest-move` (resuming) for round 3, then factory gate, then `gates.py --name 050-merge-r4` 13/13 and hand merge (S-020). |
| 053-arch-m11-rest-data | #56 | #119 | needs-human | Sync with main (head `55007c0`), `gates.py --name 053-merge-r2` 13/13, hand merge; close ARCH M11 rows in STATUS and findings.md (both halves done). |
| 056-oidc-expire-the-cached | #62 | #137 | needs-human | Sync, `gates.py --name 056-merge` 13/13, hand merge. Review file already written. |
| 057-sp2-gateway-hardening-cross | #64 | #138 | needs-human | Sync, `gates.py --name 057-merge` 13/13 (approver asked for the full suite: builder's tests/app run timed out), hand merge. |
| 058-sp2-4-follow-up | #66 | #143 | gate round 1 (builder READY, docs-only diff) | `factory/bin/gate 058-sp2-4-follow-up`, reviewer code (sonnet), approver. |

Gates strictly one at a time (chain them with `until grep -q ...; do sleep 30; done`). Then the `hold` issues: #68, #69, #71, #72, #74, #91, #93, #108, #109, #115, #123-#135, #139-#142. SP4: #126 (protected migration, hand merge under B-2705 with isolation review) before #127/#128; #141 likewise needs a lead-written migration.

S-022 (supervisor, commit `89c75da`): owner approval on 056 and 057 is recorded; they still need their 13-gate runs before the hand merge.

## Traps learned this session
- Don't leave your shell's cwd inside a job worktree: `job merge` then cannot delete it (WinError 32) after merging.
- `gh pr merge` merges with main's tip; DECISIONS.md conflicts between parallel jobs are append-only, keep both rows.
- Factory gates (core set) take ~5-10 min each alone; reviewers can run while the next gate runs.
