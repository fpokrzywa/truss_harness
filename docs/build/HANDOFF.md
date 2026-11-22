# Handoff — 2026-11-22 ~20:00 UTC, charter 7 factory run (lead session 5e01de8e)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 8 (B-2821).

## Finished this session
- Merged: #127 job 073 (PR #176, B-2825), #147 part 1 job 088 (PR #191, B-2826, issue stays open), #155 job 091 (PR #194), #188 job 090 (PR #192, B-2827; the worker-health flake is fixed, full suites are 13/13 again), #128 job 074 (PR #193, B-2828), #133 job 080 (PR #180, B-2829), #134 job 081 (PR #199, `job merge`), `lead/cm-l1-l2` (#156 L1/L2) by fast-forward to 7a0f690 (B-2830).
- Rejected: job 092 (#156 umbrella; its slices are #159-#166). Needs-info: job 094 (#158, packaging_0003 is not on main; lead work).
- Filed: #196 (startup role-check tests flaky under parallel load: `test_api_exits_3_under_owner_role_without_listening` and `test_api_serves_under_application_role`; job 095), #197 and #198 (074 isolation advisory notes).
- Decision ids: lead used B-2825..B-2830. Builders were told: 090 B-2921+, 074 B-2931+, 091 B-2941+, 093 B-2951+, 081 B-2961+, 087 B-2971+, 095 B-2981+. **Next free for the lead: B-2991**; give builders B-3001+ (step by 10).
- Lead isolation reviews saved: `docs/build/reviews/074-isolation-review.md`, `docs/build/reviews/087-isolation-review.md`.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`.
2. **093 (#157, PR #195, round 3, stage gate):** the last gate run failed only on #196 (port collision under load). Rerun `factory/bin/gate 093-sp4-10-ubuntu-host` (a rerun was in flight at handoff; check `head -1 factory/jobs/093-sp4-10-ubuntu-host/round-3/checks.md`). PASS → review (code, security) → approver (it escalated twice by rule: production deploy config + `scripts/tests/`; the lead accepts it, owner-confirm defaults stay marked) → sync DECISIONS/CLAUDE.md with main → `gates.py` → hand merge.
3. **087 (#144, PR #200, needs-human):** isolation review APPROVED. Syncing with main conflicts in `backend/tests/runtime/test_worker.py` (code, not just DECISIONS; merge aborted, branch untouched at 307bf0b). Resolve it by hand in `.worktrees/087-arch-m2-rest-2` (or `job feedback` to the builder), then `gates.py` 13/13 and hand merge. Record a B- row.
4. **095 (#196, build r1, port 6050):** builder was running. If `head -1 build.md` is READY: gate → review (code, sonnet) → approver. It changes the startup role-check test: confirm in review that it still fails when the API binds under an unsafe role (builder was told to prove it with a local revert).
5. **096 (#159 CM-1, owner priority S-025, spec):** spec-writer was running. Then builder (port 6060, B-3001+), gate, reviews, approver; isolation-critical (connections + models_admin): lead isolation review + `gates.py` before merge.
6. Hand merges still waiting, in order, each with `gates.py` (one at a time): 083 (PR #181), 084 (PR #187, after 083; both touch identity/oidc.py), 076 (PR #184, scripts only). Isolation reviews for 083 and 084 are in `docs/build/reviews/`.
7. Then 077 (#131, after 076 merges, port 5870) and the `new` CM lines #160-#163.

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 076-sp4-7-scripts-procctl | #130 | #184 | needs-human | sync, `gates.py`, hand merge |
| 077-sp4-9-go-live | #131 | - | build r1, not started | after 076 merges |
| 083-oidc-jwks-an-over | #139 | #181 | needs-human | sync, `gates.py`, hand merge |
| 084-oidc-rate-limit-jwks | #140 | #187 | needs-human | after 083 |
| 087-arch-m2-rest-2 | #144 | #200 | needs-human | step 3 |
| 093-sp4-10-ubuntu-host | #157 | #195 | gate r3 | step 2 |
| 094-sp3-follow-up-retire | #158 | - | needs-info | lead writes packaging_0003 |
| 095-flaky-test-api-exits | #196 | ? | build r1 | step 4 |
| 096-cm-1-connections-models | #159 | - | spec | step 5 |

## Traps learned this session
- Never `cd` into a job worktree in the session shell; use subshells. (WinError 32 on worktree removal otherwise.)
- Every sync with main hits a DECISIONS.md append conflict. `/tmp/lead/resolve.py` (bash path; pass it to Windows python through `cygpath -w`) has `dec` (union rows, sorted by id) and `theirs_ours` (keep main's block, then the branch's) modes. Recreate it if /tmp was cleared.
- `factory/checks.sh` and `factory/bin/gate` running beside a `gates.py` full suite make the two startup role-check tests fail (#196). Until 095 merges, rerun a gate that fails only there, ideally when no full suite runs.
- `job merge` stops at needs-human on any conflict; resolve in the worktree, push, rerun `checks.sh`, then `gh pr merge --merge --match-head-commit <sha>`.
- Approvers escalate any change under identity/, audit/, secrets/, scripts/, deploy config, core/crypto.py, the proxy, migrations or an outbound adapter move. Plan a lead isolation review plus `gates.py` for each.
