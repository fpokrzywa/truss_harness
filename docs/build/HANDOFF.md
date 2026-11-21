# Handoff — 2026-11-21 ~20:50 UTC, charter 7 factory run (lead session 8d51ac6f)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` ("Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. No agent and no gate is running. Cluster on 55450 running. `concurrency` 4 (S-018).

## Finished this session
- Merged: #46 job 049 (PR #116, `job merge`); #51 job 051 (PR #121, `job merge`, row renumbered B-2721); #33 job 043 (PR #112, hand merge B-2705, B-2727: r2 full suite found 3 real diff failures, fixed by the builder; r3 12/13 with one unrelated flake, follow-up #132); #39 job 046 (PR #114, hand merge, 13/13 4100 passed, row B-2718); #44 job 048 (PR #117, 13/13 4103 passed, row B-2719). Main's `test_every_id_taking_function_has_a_case` failure is fixed.
- SP4 go-live short design `docs/superpowers/specs/2026-11-21-subproject-4-go-live-design.md`, read by a fresh builder-reader, 31 guesses settled (section 11), open questions settled B-2723..B-2725; cut into issues #123-#131 (labels `factory`, `sp4`).
- Decision ids reserved (B-2726): 050 -> B-2720 and 053 -> B-2722 at their merges (both branches still say B-2717). 055 told to use B-2728. Next free for the lead: B-2729.
- Follow-ups filed: #132 (f09 OAuth2 fake server flake), #133 (050 clock-skew in result lookup), #134 (050 isolation notes 8-9).

## Jobs and their stages (first action: `factory/bin/job queue`)
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 050-arch-m2-rest-move | #47 | #122 | needs-human (approver ESCALATE: audit/ change, vendor call moved to worker; all 4 reviews PASS) | Charter 7.5 as the human: isolation review 1 APPROVED (`reviews/050-isolation-review-1.md`). Remaining: in its worktree renumber its DECISIONS row B-2717 -> B-2720, merge origin/main (resolve DECISIONS keeping all rows), push, `gates.py --ref <sha> --name 050-merge` (13/13), then `gh pr ready 122; gh pr merge 122 --merge --match-head-commit <sha>`. |
| 053-arch-m11-rest-data | #56 | #119 | review r1 (gate PASS) | Spawn the routed reviewers from `job.json` (none written yet). Approver will likely escalate (new data-export route) -> isolation-reviewer + gates.py; renumber its row B-2717 -> B-2722 at merge. |
| 054-f-08-follow-up | #58 | #118 | build r1 | build.md BLOCKED only because checks.sh timed out on connections; resume a builder named `builder-054-f-08-follow-up` to rerun checks.sh on a quiet cluster and set READY. |
| 055-f-10-follow-up | #60 | - | build r1 (intake ACCEPT high/security, spec written) | Spawn `builder` named `builder-055-f-10-follow-up`, port 5650. Reviewers code+security (opus). Uses B-2728. |

Then `hold` behind concurrency: #62, #64, #66, #68, #69, #71, #72, #74, #91, #93, #108, #109, #115, #123-#134. SP4 issues: do SP4-8 (#126, protected migration -> hand merge under B-2705 with isolation review) before SP4-4/5 (#127, #128).

## Traps learned this session
- Run heavy runs one at a time (gates.py full ~35-40 min, factory gate 5-15 min). Chain them in one background shell; gates.py needs timeout 7200000.
- **The factory's checks.sh core set misses diff failures in other modules**: run `gates.py` (full suite) before every hand merge and treat failures as real until shown to be outside the diff.
- `factory/bin/job` needs `PYTHONUTF8=1` (else UnicodeDecodeError on a review file).
- A reviewer once replied without writing its file; `job advance approve` refuses ("missing reviews") — always check before spawning the approver. Put "your required output is round-N/review-<r>.md; write it before replying" in reviewer prompts.
- `gh pr merge` can hit a GitHub TLS timeout; just retry.
- Builders stall (stream watchdog) after pushing: check `git log origin/<branch>` and `head -1 build.md` before respawning.
