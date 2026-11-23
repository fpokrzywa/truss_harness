# Handoff — 2026-11-23 ~14:55 EST, charter 7 factory run (lead session 58242b09)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (sub-projects table), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. Lead context near 200k; no agent running at handoff.

## Finished this session
- Merged 146 SP6-2 follow-up (#300, PR #309, 13/13) and 133 SP5-2 appgen skeleton (#255, PR #298, 13/13 on r3).
- 140 SP8-8 Docker: rounds 4-6 (YAML `$` escapes, alias cycles, non-plain YAML types, Docker Engine 28.0 floor, label allow-lists, health recheck). Lead isolation review 2 BLOCKED -> feedback-2 -> review 3 APPROVED. Records `docs/build/reviews/140-isolation-review-{2,3}.md`.
- 143 #285 round 3 reviews PASS, approver ESCALATE; isolation APPROVED (`reviews/143-isolation-review-1.md`). Follow-up #312 filed (factory label; depends on #285 merged).
- 145 SP6-3 round 2 (claim identity from #300, locator 422 = branch B-3947) reviews PASS, approver ESCALATE; isolation APPROVED (`reviews/145-isolation-review-1.md`).
- Decisions B-3948 (Docker 28.0), B-3949 (143 over-refusals, #312), B-3950 (gate flakes under parallel load). **B-3947 is taken on 145's branch. Next free lead id: B-3951.**

## Exact next step: merge the six needs-human jobs, one at a time
Each is lead-isolation APPROVED and reviews PASS. For each: sync branch with origin/main in its worktree (DECISIONS: keep both, main first; alembic-heads lists in `tests/identity/test_schema.py`: include every head, never drop an assertion), push, `python scripts/gates.py --ref <sha> --name <id>-merge` (serial! nothing else heavy running — parallel load caused 2 flakes, B-3950), then `gh pr merge <pr> --merge --match-head-commit <sha>`. A general-purpose sonnet agent did sync+gates well this session (prompt: sync, run touched tests, push, gates.py, report gate lines).
Order (suggested): 139 (PR #302) -> 141 (PR #307; file follow-up: refuse `:<digit>` in the Snowflake reading) -> 137 (PR #310; now that 133 merged, its schema test must expect both `appgen_0001` and `mcp_0001` heads — fix in the sync; file follow-ups limiter order, docstring, removed-member key test) -> 145 (PR #311; add notes to B-3934/B-3936 that B-3947 supersedes them) -> 140 (PR #303) -> 143 (PR #308).
After each merge run `PYTHONUTF8=1 factory/bin/job queue` (it marks the job merged).

## Open jobs
- 147-sp5-3-appgen-accept (#256): intake ACCEPT (risk high, security), routed, stage **spec**: spawn `spec-writer` with the job header.
- #257 SP5-4 appgen-web: lead must first add appgen-web lines to protected `factory/checks.sh`, then `job create 257`.
- #258 depends on #256 and #257; #259 on #258; #260 on #256+#257; #261 on #259+#260; #262 on #261. SP6-4 #271..#275 wait on #262. #312 waits on #285 (job 143). Other holds per dependency rules in the previous handoff (SP7: #277 after #255 — now startable; #278 after #277. SP8: #286 after #270+#271; #288 after #287+#279-#281; #290/#292/#294 per body).

## Traps learned
- `gates.py` writes no `.summary` file any more; read the `gates: N/13 passed` stdout line (redirect stdout to a file).
- Do not run `factory/bin/gate` while a `gates.py` full suite runs (flakes in timing tests).
- Earlier traps hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, job commands block during `job merge`, builders may stall after pushing, isolation reviewers do not write files).
