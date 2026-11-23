# Handoff — 2026-11-23 ~01:30 UTC, charter 7 factory run (lead session f36bd0f4)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 8. Handed off at the context limit (~185k), no agent running.

## Finished this session
- Merged: #160 CM-2 (job 097, PR #207, hand), lead migrations PR #214 (hand; #183 and #158 closed), #208 (job 115, PR #219, `job merge`), #205 (job 114, PR #220, `job merge`), #202 (job 113, PR #216, hand, supervisor approval S-033; supervisor told it can re-run hosted setup).
- 094 rejected at intake (duplicate of PR #214), #158 closed.
- Follow-ups filed: #215 (restore check pins context-helper bodies; needs 111's `catalog_rules.py` on main, so start it only after PR #213 merges), #218 (BYPASSRLS guard for `rewrap_all`, `_check_audit`, restore tenant listing; touches `secrets/` and `cli/backup.py`, so start after 112/PR #217 merges).
- Lead decision B-2998 (job 110): the console polls the new invocation status route, and the parsed request goes in `jobs.payload` with no secret in it (a test must show that). Posted on #190. The builder writes the DECISIONS row on its branch. **Next free lead id: B-2999.** Builder ids given: 112 B-3091+, 113 B-3101+, 098 B-3111+, 114 B-3121+, 115 B-3131+, 110 B-3141+; **give new builders B-3151+.**

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue` (300 s timeout).
2. **098 (#161, CM-3, PR #221, build r2):** round 1 code review CHANGES (read-only check bypass: Databricks raw strings `R"\"; DROP ..."` pass `_code_of` in `adapters/dataplatforms/sqlapi.py`), security PASS (non-blocking: legacy `databricks` rows with an unchecked host; validate the host in `DatabricksSession.__init__` or before `discover`/`open`; fold it into round 2). Spawn `builder` named `builder-098-cm-3-adapters-data`, told it is resuming, with header + `Build round 2. Port: 6080.` + `Round 1 reviews are in round-1/. Address every VERDICT: CHANGES, also validate the stored host before discover/open (security note), push, and update build.md for round 2.` Then gate → reviewers → approver. Isolation-critical: expect ESCALATE; then lead runs `isolation-reviewer` on the PR and a sync + `gates.py` 13/13 before a hand merge. After 098 merges: start 099 (CM-4 Snowflake, port 6090), then 100 (CM-5, port 6100); 102 CM-7 / 103 CM-8 re-intake (`job advance <id> intake`) once CM-3/CM-4 merge.
3. **110 (#190, PR #222, stage gate r1):** builder READY on `9ae3c00` (invoke queued as `connection.invoke`, requester-only status route, console polls it). Run `factory/bin/gate 110-arch-m2-rest-3`, then reviewers code+security (opus) → approver. Isolation-critical (connections, worker): lead `isolation-reviewer` before a hand merge. Possible follow-ups from build.md: principals with only `connection:use` cannot read their own invocation (status route needs `connection:read`); parameter values stay in `jobs.payload` (request-retention scrub).
4. **Hand merges, one at a time** (each: one general-purpose sonnet agent merges origin/main into the job worktree with `git -C`, resolves DECISIONS with `python /tmp/lead/resolve.py dec <file>`, regenerates `backend/openapi.json` with `uv run python -m tests._support.openapi_snapshot` instead of hand-merging it, runs scan-secrets, pushes without force, waits for no other `gates.py`/`job merge`, runs `gates.py --ref <sha> --name <job>-merge` with a background timeout ≥ 3600000 ms, and reports in ≤ 25 lines; then lead `gh pr merge <n> --merge --match-head-commit <sha>`). Order: 077 (PR #206), 105 (PR #209), 083 (PR #181), 084 (PR #187), 109 (PR #211; after 083/084, all touch identity/oidc.py), 087 (PR #200; `backend/tests/runtime/test_worker.py` conflict by hand), 111 (PR #213; lead accepts option (a), then B-3081 schema-test move), 112 (PR #217). Isolation reviews for 083, 084, 087 are in `docs/build/reviews/`; 112 touches `core/dbrole.py` and `secrets/service.py`: run `isolation-reviewer` on it first.
5. Lead work outstanding (protected paths): #174 items 1 (retention-delete guard hardening or maintenance role, migration), 3 (`deliberate_changes` reason for runtime_0002), 4 (evaluation pin); B-3081 schema-test move onto `shifde.cli.catalog_rules`.
6. Then the `hold`/`new` lines (#215 after 111, #218 after 112).

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 077-sp4-9-go-live | #131 | #206 | needs-human | step 4 |
| 083-oidc-jwks-an-over | #139 | #181 | needs-human | step 4 |
| 084-oidc-rate-limit-jwks | #140 | #187 | needs-human | step 4, after 083 |
| 087-arch-m2-rest-2 | #144 | #200 | needs-human | step 4 |
| 098-cm-3-adapters-data | #161 | #221 | build r2 | step 2 |
| 099-cm-4-adapters-snowflake | #162 | - | build r1, held | after 098 merges |
| 100-cm-5-live-tests | #163 | - | build r1, held | after 098+099 |
| 102-cm-7-front-end | #165 | - | needs-info | after CM-3/4 |
| 103-cm-8-scripts-scripts | #166 | - | needs-info | after CM-1..7 |
| 105-harden-retention-delete-guard | #174 | #209 | needs-human | step 4 |
| 109-oidc-the-idp-test | #189 | #211 | needs-human | step 4, after 084 |
| 110-arch-m2-rest-3 | #190 | #222 | gate r1 | step 3 |
| 111-backup-restore-check-restored | #197 | #213 | needs-human | step 4 |
| 112-secrets-check-fail-when | #198 | #217 | needs-human | step 4 |

## Traps learned this session
- `gates.py` takes ~25 min; run one at a time. Agents that start it with a 10-minute background timeout must stop and relaunch it, and a stopped run can leave a stale `.git/worktrees/<name>/index.lock` (remove it only when no git process runs).
- `factory/bin/job merge` and a hand merge racing on main: wait for one to finish before merging the other, then re-check that the PR is still mergeable (`gh pr view <n> --json mergeable`; UNKNOWN right after a push means try again shortly).
- `alembic heads` CLI does not work (no `alembic.ini`); use `shifde.core.migrations.code_heads()`.
- `cd` into a worktree in Bash persists; always `cd` back to the main checkout or use `git -C`.
- A sync agent that is waiting on its own background `gates.py` keeps sending "waiting" notifications; stop it with TaskStop once its report has arrived.
- Earlier traps (mypy INTERNAL ERROR → delete `.mypy_cache`; eslint missing → `pnpm install --frozen-lockfile`; approvers escalate `Closes #n` when intake narrowed scope; builders can stall after READY) still hold.
