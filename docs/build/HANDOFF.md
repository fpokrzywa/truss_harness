# Handoff — 2026-11-22 ~23:50 UTC, charter 7 factory run (lead session a31179bf)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 8. Handed off at the context limit (~190k), no agent running.

## Finished this session
- Merged: #157 job 093 (PR #195, hand merge after `gates.py --name 093-merge` 13/13), #167 job 104 (PR #210, `job merge`), #185 job 108 (PR #212, `job merge`).
- Rejected at intake: 106 (#175 already fixed on main; issue closed), 107 (#183 migration only; issue stays open until PR #214 merges, then close it).
- Lead migrations on branch `lead/migrations-1` (worktree `.worktrees/lead-mig1`), **PR #214**: audit_0002 (#183), packaging_0003 (#158), connections_0003 (#190). Targeted tests, ruff, mypy, lint-imports pass; isolation review APPROVED (`docs/build/reviews/lead-migrations-1-isolation-review.md`); B-2993..B-2997. Note: `shifde-admin db heads` (named in #158's acceptance) does not exist; alembic heads and `db check` show packaging_0003.
- 097 lead isolation review APPROVED (`docs/build/reviews/097-isolation-review.md`), follow-up issue #208 filed. Lead accepts the approver's escalation points (check_value wrapper; rotation scope = B-2749).
- Decision ids: lead used B-2993..B-2997. **Next free for the lead: B-2998.** Builders were given 104 B-3041+, 105 B-3051+, 109 B-3061+, 108 B-3071+, 111 B-3081+; **give new builders B-3091+** (step 10).

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue` (300 s timeout).
2. **097 (PR #207, needs-human):** a sonnet agent merged origin/main into the branch and ran `gates.py --name 097-merge` 13/13 on `a6fa445` (PR marked ready), but GitHub then refused the merge: conflicts with main after 104 (PR #210) and 108 (PR #212) merged (likely DECISIONS.md, CLAUDE.md or backend/openapi.json; regenerate openapi.json with `uv run python -m tests._support.openapi_snapshot` rather than hand-merging it). Re-sync, re-run `gates.py --name 097-merge2`, then `gh pr merge 207 --merge --match-head-commit <head>`. Do this first: CM-3..CM-5 wait on it. Then CM-3 (098) builder can start (port 6080, held so far), later 099 (6090) and 100 (6100).
3. **Hand merges, one at a time**, each: sync with main (DECISIONS conflicts: `/tmp/lead/resolve.py dec`), `gates.py` 13/13, `gh pr merge --merge --match-head-commit`. Delegate each sync+gates to one general-purpose sonnet agent (brief: merge origin/main in the job worktree with `git -C`, resolve DECISIONS with resolve.py, scan-secrets, push without force, wait until no other gates.py runs, run `gates.py --ref <sha> --name <job>-merge`, report ≤25 lines). Order: PR #214 (lead migrations; afterwards close #183, re-run intake for 110/#190 via `job advance 110-arch-m2-rest-3 intake`, and let 094/#158 re-intake for its factory test part), 077 (PR #206), 105 (PR #209), 083 (PR #181), 084 (PR #187), 109 (PR #211; after 083/084, all three touch identity/oidc.py), 087 (PR #200; `backend/tests/runtime/test_worker.py` conflict by hand). Isolation reviews for 083, 084, 087 are in `docs/build/reviews/`.
4. **111 (#197, PR #213, stage review r1):** gate PASS. Spawn reviewer-code (opus) and reviewer-security (opus) → approver. Expect ESCALATE (backup/restore, ported schema rules; follow-up to move `tests/schema/*` onto `src/shifde/cli/catalog_rules.py` is lead work, B-3081).
5. Lead work outstanding (protected paths): #174 items 1 (retention-delete guard hardening or maintenance role, migration), 3 (`deliberate_changes` reason for runtime_0002), 4 (evaluation pin); B-3081 schema-test move.
6. Parked needs-info: 102 CM-7 and 103 CM-8 (queue says "author replied" but it only sees the bot's own comment; re-intake once CM-3/CM-4 merge), 094 (#158, after PR #214), 110 (#190, after PR #214).
7. Then the `hold` lines (#198, #202, #205, #208).

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 077-sp4-9-go-live | #131 | #206 | needs-human | step 3 |
| 083-oidc-jwks-an-over | #139 | #181 | needs-human | step 3 |
| 084-oidc-rate-limit-jwks | #140 | #187 | needs-human | step 3, after 083 |
| 087-arch-m2-rest-2 | #144 | #200 | needs-human | step 3 |
| 094-sp3-follow-up-retire | #158 | - | needs-info | after PR #214 |
| 097-cm-2-connections-admin | #160 | #207 | needs-human | step 2 |
| 098-cm-3-adapters-data | #161 | - | build r1, held | after 097 merges |
| 099-cm-4-adapters-snowflake | #162 | - | build r1, held | after 098 |
| 100-cm-5-live-tests | #163 | - | build r1, held | after 098+099 |
| 102-cm-7-front-end | #165 | - | needs-info | after CM-3/4 |
| 103-cm-8-scripts-scripts | #166 | - | needs-info | after CM-1..7 |
| 105-harden-retention-delete-guard | #174 | #209 | needs-human | step 3 |
| 109-oidc-the-idp-test | #189 | #211 | needs-human | step 3, after 084 |
| 110-arch-m2-rest-3 | #190 | - | needs-info | after PR #214 |
| 111-backup-restore-check-restored | #197 | #213 | review r1 | step 4 |
| lead/migrations-1 | #183 #158 #190 | #214 | lead PR | step 3 first |

## Traps learned this session
- Parallel factory gates can break each other: one gate hit a mypy INTERNAL ERROR (fixed by deleting the worktree's `backend/.mypy_cache` and re-gating), another saw `eslint` missing (re-run `pnpm install --frozen-lockfile` in the worktree's `frontend/`, then re-gate). Neither is a code failure.
- Approvers escalate PRs whose body says `Closes #n` when intake narrowed scope; edit the body to `Refs #n` and comment what remains on the issue.
- A builder can stall after it has pushed and reported READY; check `build.md` first line and the PR, then advance normally.
- `cd` into a worktree in Bash persists the session's working directory; always `cd` back to the main checkout or use `git -C`.
- Earlier traps (never `cd` into job worktrees for edits; DECISIONS append conflicts; `job merge` stops at needs-human on conflict; queue can take >120 s) still hold.
