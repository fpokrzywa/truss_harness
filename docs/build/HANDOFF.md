# Handoff — 2026-11-22 ~14:00 UTC, charter 7 factory run (lead session 1174d2af)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. `concurrency` 4 (S-028). No agent and no gate of this session is running. Handed off at the context limit (~240k).

## Finished this session
- Merged: #108 job 066 (PR #153), #93 job 065 (PR #154, rounds 1-2, isolation reviews 1-2), #109 job 067 (PR #168), #124 job 070 (PR #169). Evidence in STATUS and B-2742.
- #156 design written, builder-read, settled (B-2743..B-2759); issues #159 (CM-1) .. #166 (CM-8) filed, labels `factory` + `connections`.
- Filed #158 (retire guard migration `packaging_0003`, lead-written) and #167 (065 follow-ups: pipeline node connection ids, draft round trip).
- Decision ids used: up to B-2759 (lead), B-2770..B-2783 (jobs; 069 used B-2774..B-2778, 071 B-2780, 072 B-2783). **Next free for the lead: B-2784** (skip any id a job branch already holds: `git show origin/<branch>:docs/build/DECISIONS.md | grep -o '^| B-27[0-9][0-9]'`).

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`, then resume each job below (gates one at a time, never two at once).
2. **Owner priority S-025:** finish L1/L2 so CM-1 (#159) can start before other `new` lines.

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 068-test-fixtures-sweep-shifdet | #115 | #170 | review r1 (gate PASS) | spawn `reviewer-code` (opus), then approver |
| 069-sp4-1-core-app | #123 | #171 | gate r1 | `factory/bin/gate`; reviewers code+security (opus) |
| 071-sp4-3-front-end | #125 | #172 | gate r1 | `factory/bin/gate`; reviewers code, ux (opus), ui (sonnet), security (opus) |
| 072-sp4-8-retention-delete | #126 | #173 | gate r1 | Gate FAILS until the lead commits the 20 `tests/schema/deliberate_changes.yaml` entries listed word for word in DECISIONS B-2783 on the branch (B-2530/B-2782). Commit them on `factory/072-sp4-8-retention-delete`, push, then gate. Review note: the branch changes `tests/ledger/test_reservations.py` to expect DELETE granted on `budget_reservations` (required by the issue; guarded by `retention_delete_guard`); the isolation review must rule on it. Protected migration `runtime_0006`: hand merge under B-2705 after `gates.py` 13/13 + isolation review. |
| (new) | #127, #128 and onward | - | queue | `factory/bin/job create <n>`; but start CM-1 (#159) first once L1/L2 is merged. |

## Branch `lead/cm-l1-l2` (worktree `.worktrees/cm-l1-l2`, DB `shifde_cm_l1_l2`)
- Commits `17631d8` (L1 `connections_0002`, L2 `models_admin_0004`, five yaml entries, `tests/connections/test_scope_guard_db.py`) and `a4ff042` (fixtures bind secrets of the connection's scope; no assertion changed). Pushed, not merged.
- Builder departures to accept or not (log as B-2784+): L1 restores the `search_path` pin of `connections_guard` (B-1714) in both directions; L2 uses `IF EXISTS (... workspace_id IS NOT NULL)` instead of `NOT EXISTS (... IS NULL)` so cross-client tests keep their 42501/23503; L2 guard also pins `search_path`.
- Full suite: 4271 passed, 1 failed: `tests/app/test_r1_critic.py::test_a_workspace_administrator_cannot_repoint_a_connection_on_a_tenant_scoped_secret`. Its setup (workspace connection on a client-wide secret) is now impossible under L1. It is a security test: do not delete it or loosen its assertion. Decide how its setup reaches the same refusal (e.g. a workspace connection repointed to a secret of another scope the admin cannot use) and record the call.
- Then: isolation review, `python scripts/gates.py --ref <sha> --name cm-l1-l2-merge` 13/13 in an agent, merge to main (fast-forward or PR, no force push). CM-1's 23514 -> 422 mapping (B-2745) is not in this branch.

## Traps learned this session
- Every merge of a job branch hits a `docs/build/DECISIONS.md` append conflict. Before `job merge`, sync the branch: merge origin/main, keep both sides' rows sorted by id, push (`job merge` then syncs cleanly). After a `job merge` that stops at needs-human for this reason, sync, `bash factory/checks.sh` in the worktree, then `gh pr merge <n> --merge --match-head-commit <sha>`.
- `pgrep` does not exist in Git Bash here.
- The L1/L2 agent saw the local cluster's app-role password once in its own tool output (exporting `.devdata/dev.env` before a core test). It is in no file or commit. Do not export dev.env before `tests/core`.
- 068's sweep dropped ~2,200 stale `shifdet_` databases from the shared cluster on its first runs; gates should be faster now.
