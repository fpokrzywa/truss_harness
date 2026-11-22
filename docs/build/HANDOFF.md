# Handoff — 2026-11-22 ~05:50 UTC, charter 7 factory run (lead session 54dad67b)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 2 (S-021). No agent and no gate of this session is running at handoff. Lead context reached 168k (limit 200k), so this session stops between jobs.

## Finished this session
- Merged: #66 job 058 (PR #143, `job merge`, `dccf25a`); #62 job 056 (PR #137, hand `f4e0dc7`); #64 job 057 (PR #138, hand `220cb6d`); #47 job 050 (PR #122, hand `526081e`); #56 job 053 (PR #119, hand `963a795`); #68 job 059 (PR #145, `job merge`, `86f9b39`). Hand-merge evidence in B-2731.
- ARCH M11 closed (STATUS and findings.md). Item 21 (ARCH M2) still narrowed; remaining families filed as **#144** (factory label).
- Job 060 (#69, PR #146): gate PASS, code+security PASS, approver ESCALATE by rule (changes who may approve a plan; no defect). Isolation review 1 APPROVED (`docs/build/reviews/060-isolation-review-1.md`). Gaps filed as **#147**. Stage needs-human.
- Decision ids: next free for the lead is **B-2734**.

## Jobs and their stages (first action: `PYTHONUTF8=1 factory/bin/job queue`)
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 060-sp2-5-follow-up | #69 | #146 | needs-human | Needs the supervisor's (owner) acceptance of the related-principal rule (requester, service account owned by requester, owner of requesting service account; gaps in #147). Then sync with main, `gates.py --name 060-merge` 13/13, hand merge, record. |
| (new) | #71, #72 | - | queue shows `new` | `factory/bin/job create 71` and `72`, start at intake. |

Then the `hold` issues in queue order: #74, #91, #93, #108, #109, #115, #123-#135, #139-#142, #144, #147. SP4: #126 (protected migration, hand merge under B-2705 with isolation review) before #127/#128; #141 needs a lead-written migration.

## Traps learned this session
- Postgres checkpoints stall under parallel load: every `DROP DATABASE ... WITH (FORCE)` waits on a checkpoint (`IPC CheckpointDone`, checkpointer in `IO DataFileSync`). A full-suite `gates.py` run plus a `job merge` (which runs `jobdb drop`) or `job queue` (it drops merged jobs' databases) at the same time made the drops wait 10-25 min, `job queue` hang >10 min, and one test hit `psycopg ConnectionTimeout` at setup. It is not a deadlock; it drains. Run gates strictly one at a time and don't run `job queue` while a full suite runs.
- The Bash tool's background limit kills only the bash wrapper; the Windows python child (`job merge`) keeps running. Check with `Get-CimInstance Win32_Process` before restarting anything.
- `gates.py` writes its summary to the redirected file only at the end; progress is the newest file in `.worktrees/_gate-logs/<name>/`.
- After a hand merge, a branch synced with main conflicts on `docs/build/DECISIONS.md` (append-only: keep both rows) and on `UNIFIED_ONLY` in `backend/tests/app/test_routes.py` (keep both entries). After a sync, run `bash factory/checks.sh` in the job worktree (~5-17 min) rather than a second full `gates.py`, and check the OpenAPI snapshot when `openapi.json` auto-merged.
- isolation-reviewer is read-only: it returns the review text; the lead saves it to `docs/build/reviews/`.
