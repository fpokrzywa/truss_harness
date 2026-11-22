# Handoff — 2026-11-22 ~22:30 UTC, charter 7 factory run (lead session 7d438bdc)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 8 (B-2821). Handed off at the context limit (~185k), no agent running.

## Finished this session
- Merged: #196 job 095 (PR #201, `job merge`; startup role-check load flake fixed), #164 job 101 CM-6 (PR #204, `job merge`), #130 job 076 (PR #184, hand merge B-2991), #159 job 096 CM-1 (PR #203, hand merge B-2992, lead isolation review `docs/build/reviews/096-isolation-review.md`).
- Filed #205 (096 isolation notes: cross-client tests for `include_shared`/`last_test_outcome`; precise 23514 codes).
- Decision ids: lead used B-2991, B-2992. Builders told: 096 B-3001+, 101 B-3011+, 077 B-3021+, 097 B-3031+. **Next free for the lead: B-2993**; give builders B-3041+ (step by 10).

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue` (it takes ~2 min; give it a 300 s timeout).
2. **097 (#160 CM-2, PR #207, stage review r1):** gate PASS (233 s). Spawn reviewers code+security (opus) → approver. Isolation-critical (connections, secrets, admin): lead isolation review + sync + `gates.py` before hand merge. Then CM-3 (098) builder can start.
3. **077 (#131, PR #206, stage review r1):** gate PASS (271 s). Spawn reviewer-code and reviewer-security (opus) → approver. It edits `scripts/`, CLAUDE.md, golive-register (closed Item 5a / Item 20 delete) and `backend/tests/cli/test_config_preflight.py` (two expected states only): expect ESCALATE; lead checks the register change, then sync, `gates.py`, hand merge.
4. **104 (#167, stage spec):** spawn spec-writer, then builder (port 6140, B-3041+).
5. **Hand merges still waiting**, one at a time, each: sync with main (DECISIONS conflicts: `/tmp/lead/resolve.py dec`), `gates.py` 13/13, `gh pr merge --merge --match-head-commit`: 093 (PR #195, escalated: production hosting config; owner-confirm defaults stay marked), 083 (PR #181), 084 (PR #187, after 083; both touch identity/oidc.py), 087 (PR #200; `backend/tests/runtime/test_worker.py` conflict to resolve by hand). Isolation reviews for 083, 084, 087 are in `docs/build/reviews/`. Delegate each sync+`gates.py` to one general-purpose sonnet agent (brief used this session: merge origin/main, resolve DECISIONS with resolve.py, push, wait for any other `gates.py` to finish, run `gates.py`, report ≤25 lines).
6. Held until their dependencies merge (builders NOT started; stage `build` r1, specs written): 098 CM-3 (#161, after CM-2), 099 CM-4 (#162, after CM-3), 100 CM-5 (#163, after CM-3+CM-4). Parked needs-info: 102 CM-7 (#165, after CM-3/4/6; queue shows "author replied" — it only sees the bot's own comment; re-run intake only once CM-3/CM-4 merge), 103 CM-8 (#166, after CM-1..7), 094 (#158, lead writes packaging_0003).
7. Then the `hold` lines (#174, #175, #183, #185, #189, #190, #197, #198, #205).

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 077-sp4-9-go-live | #131 | #206 | review r1 | step 3 |
| 083-oidc-jwks-an-over | #139 | #181 | needs-human | step 5 |
| 084-oidc-rate-limit-jwks | #140 | #187 | needs-human | step 5, after 083 |
| 087-arch-m2-rest-2 | #144 | #200 | needs-human | step 5 |
| 093-sp4-10-ubuntu-host | #157 | #195 | needs-human | step 5 |
| 094-sp3-follow-up-retire | #158 | - | needs-info | lead writes packaging_0003 |
| 097-cm-2-connections-admin | #160 | #207 | review r1 | step 2 |
| 098-cm-3-adapters-data | #161 | - | build r1, held | after 097 merges (port 6080) |
| 099-cm-4-adapters-snowflake | #162 | - | build r1, held | after 098 (port 6090) |
| 100-cm-5-live-tests | #163 | - | build r1, held | after 098+099 (port 6100) |
| 102-cm-7-front-end | #165 | - | needs-info | after CM-3/4 |
| 103-cm-8-scripts-scripts | #166 | - | needs-info | after CM-1..7 |
| 104-sp3-follow-up-hide | #167 | - | spec | step 4 |

## Traps learned this session
- A subagent's own background `gates.py` survives the agent's turn; it reports back later via message. Don't start a second copy; `.worktrees/_gate-logs/<name>/` shows progress.
- The isolation-reviewer refuses to write files (its standing instructions); write its review into `docs/build/reviews/` yourself from its report.
- Intake agents for dependent CM slices read main and report "dependency not merged"; that is expected — advance them and hold the builder, don't re-intake.
- `factory/bin/job queue` can exceed 120 s while a merge holds the lock; run it with a 300 s timeout.
- Earlier traps (never `cd` into job worktrees; DECISIONS append conflicts; `job merge` stops at needs-human on conflict) still hold, see the previous handoff in git history.
