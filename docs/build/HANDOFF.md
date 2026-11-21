# Handoff — 2026-11-21 ~16:30 UTC, charter 7 factory run (lead session 15c9184a)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. No agent and no gate is running. Cluster on 55450 running. Factory `concurrency` is now **4** (S-018, supervisor); more than 4 jobs are already open, so `job queue` will not offer `new` lines until some finish.

## Finished this session
- #41 job 047 merged (PR #113, `job merge`).
- #55 job 052 merged (PR #120, code+security PASS, approver APPROVE, `job merge`). Note: `ModelService._trust_gate` now rolls back the caller's session before its denied record (same as `_shi_gate`).
- Intake+spec for jobs 048-054; builds for all of them.
- Committed the two isolation reviews the previous lead recorded: `reviews/043-isolation-review-1.md`, `reviews/046-isolation-review-1.md` (both APPROVED).

## Jobs and their stages (first action: `factory/bin/job queue`)
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 043-f-12-follow-up | #33 | #112 | build r1 (READY; hand merge, B-2705) | Branch synced to main at `f7fadcc` (DECISIONS conflict resolved, both rows kept), now behind again: merge origin/main, run `pytest tests/schema tests/identity/test_schema.py`, then `gates.py --ref <sha> --name 043-merge` **on a quiet cluster**. Code/security/isolation all PASS/APPROVED. Merge with `gh pr ready 112; gh pr merge 112 --merge --match-head-commit <sha>`. This also fixes main's failing `test_every_id_taking_function_has_a_case`. Merge it first. |
| 046-tenants-settings-restrict-app | #39 | #114 | build r1 (READY; hand merge, B-2705) | Lead deliberate_changes entry committed `d72d4dc`; isolation APPROVED; code+security PASS. Merge origin/main, schema tests, `gates.py --name 046-merge`, hand merge as above. |
| 048-worker-cap-abandoned-wedged | #44 | #117 | needs-human (approver APPROVE; `job merge` hit a conflict with main) | Conflict is DECISIONS.md only (several jobs used B-2717). As the human: merge origin/main into the branch keeping all rows, renumber this job's row to the next free id, push, then `job feedback`-free path: `gates.py --name 048-merge` and `gh pr merge 117 --merge --match-head-commit <sha>`, or send feedback through `factory/bin/job feedback` for a round that only resolves the conflict. |
| 049-retention-purge-second-cross | #46 | #116 | gate r1, FAIL twice on connection timeouts only (no diff failure; diff is one comment + one test) | Rerun `factory/bin/gate 049-...` alone on a quiet cluster. |
| 050-arch-m2-rest-move | #47 | #122 | gate r1 (build READY) | Run `factory/bin/gate`. Reviewers code, ux, ui, security. Approver will likely escalate (POST 200 -> 202, worker dispatch). Uses B-2717 too: renumber at merge. |
| 051-flaky-test-write-node | #51 | #121 | gate r1 (build READY) | Run `factory/bin/gate`. |
| 053-arch-m11-rest-data | #56 | #119 | gate r1 (build READY) | Run `factory/bin/gate`. Security reviewer routed; approver will likely escalate (new data-export route); then isolation-reviewer + gates.py per charter 7.5. |
| 054-f-08-follow-up | #58 | #118 | build r1, build.md says BLOCKED only because checks.sh timed out on connections (test-only diff) | Do not send to needs-human: resume `builder-054` to rerun checks.sh on a quiet cluster and set READY. |

Still `hold` behind concurrency: #60, #62, #64, #66, #68, #69, #71, #72, #74, #91, #93, #108, #109. Then sub-project 4 (charter 7 steps 1-2, design row 4, go-live; all major audit findings closed or awaiting owner acceptance by its end).

## Traps learned this session
- **Run gates one at a time.** Any full checks.sh/gates.py run alongside other jobs' test runs times out on connections (hundreds of ConnectionTimeout lines). Builders also run checks.sh; with several builders active the cluster saturates. Chain gate and merge commands serially in one background shell (`until grep -q "GATE:" <prev output>; do sleep 30; done; ...`).
- **Decision ids collide across parallel jobs.** 046, 048, 052, 053 each wrote B-2717 (051 wrote B-2715, also used by 043). At each merge, renumber the merging job's row to the next free id on main.
- Builders often stall (stream watchdog, 600 s) *after* writing build.md and pushing; check `head -1 build.md` and `gh pr list --head <branch>` before respawning.
- intake once claimed it could not write intake.md; a SendMessage saying it is the required output fixed it.
- gates.py background runs need a timeout above 90 min under load (max 7200000 ms).
- All traps in the previous handoff still apply (PYTHONUTF8=1, unset VIRTUAL_ENV, strip .venv from PATH; never cd into a worktree; isolation-reviewer is read-only so the lead writes its file).
