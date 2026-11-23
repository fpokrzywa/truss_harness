# Handoff — 2026-11-23, charter 7 factory run (lead session a0eef6a0)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (sub-projects table), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. Lead context passed 200k; no new agents were started after that, and the running ones finished their step.

## Finished this session
- Merged: 136 SP7-5 board API (#281, PR #299), 142 SP7-6 board screen (#282, PR #301), 144 flaky ActionsEditor test (#305, PR #306), 135 SP6-2 Snowflake native adapter (#269, PR #297, hand merge `3c4b526`, B-3938).
- Main fix `6400ae6`: `test_the_committed_register_parses` pinned to the owner-accepted register states (B-3939).
- Isolation reviews written: `docs/build/reviews/133-isolation-review-1.md` (round 1 BLOCKED, round 2 APPROVED), `135-`, `138-`, `139-isolation-review-1.md` (APPROVED).
- Issues filed: #300 (SP6-2 isolation follow-up, job 146), #305 (flaky test, merged). Wiring preconditions posted as comments on #290 (Fabric, SP8-5) and #292 (Palantir, SP8-7): those jobs' specs must carry them.
- Decisions B-3937..B-3943. **Next free lead id: B-3944.** Job branches carry their own rows: 143 and 146 both B-3926, 145 B-3930..B-3936 (lead rows were renumbered to B-3937..B-3943 to avoid 145). When two merged rows share an id, renumber the later one (keep both, main first).

## Open jobs (`PYTHONUTF8=1 factory/bin/job queue`)
- **133-sp5-2-appgen-skeleton** (#255, PR #298): needs-human. Isolation APPROVED (round 2). `gates.py --name 133-merge` on `75e7fe4` 12/13: `tests/identity/test_schema.py::test_a_head_named_in_depends_on_is_not_a_version_head` fails because migration `appgen_0001` names `identity_0007`, a version head, in `depends_on`. Real, inside the job (supervisor, 12:07 EST: not a flake). Supervisor's instruction: in job 133, update that test's expected code heads and its docstring to match the branch (`appgen_0001` now depends on `identity_0007`, so `identity_0007` is no longer a head; the test is not a protected path), keeping what the test asserts about `depends_on` heads; send it with `factory/bin/job feedback 255`, then gate, review, lead `gates.py` and hand merge (`gh pr merge --match-head-commit`). The same applies to job 137 (`mcp_0001_access` also depends on `identity_0007`) and to whichever of them merges second. #256 and #257 wait on it.
- **139-sp8-6-adapters-native** (#291, PR #302): needs-human. Isolation APPROVED, synced with main at `0badbde` (666 affected tests passed). Next: `python scripts/gates.py --ref 0badbde6b7a534727829b95c61217fad0c8c2e49 --name 139-merge` (re-sync first if main moved), then hand merge.
- **138-sp8-4-adapters-native** (#289, PR #304): stage review round 3, gate PASS. Spawn code+security; expect ESCALATE (vendor) -> isolation already APPROVED (round-2 fixes are in) -> `gates.py` -> hand merge.
- **140-sp8-8-adapters-native** (#293, PR #303): stage review round 2, gate PASS (security round-1 CHANGES fixed). Spawn code+security; expect ESCALATE -> isolation review -> `gates.py` -> hand merge. Code-review notes for SP8-9 (in-process fake gate, verify port, daemon id at observe) belong on #294.
- **141-sp6-1-sql-tools** (#268, PR #307): stage review round 1, gate PASS. Spawn code+security.
- **143-read-only-sql-check** (#285, PR #308): stage review round 1, gate PASS. Spawn code+security.
- **146-sp6-2-follow-up** (#300, PR #309): stage review round 1, gate PASS. Spawn code+security. Must merge before 145.
- **137-sp8-2-shifde-mcp** (#287, PR #310): stage review round 1, gate PASS: spawn code, ux, ui, security reviewers. Its migration `mcp_0001_access` also lists `identity_0007` in `depends_on`: check it against `tests/identity/test_schema.py::test_a_head_named_in_depends_on_is_not_a_version_head` (the defect that failed job 133) before the lead `gates.py`.
- **145-sp6-3-delivery-snowflake** (#270, PR #311): READY, stage gate round 1 (gate not yet run): run `factory/bin/gate 145-sp6-3-delivery-snowflake`. Adds protected migration `delivery_0007_snowflake_kinds` (check it against the `depends_on` head test too). Merges only after 146 (#300); then switch its claim key (`delivery.service.claim_key`, `TODO(#300)`) and `executor/credentials.py` `snowflake_account()` to #300's derivation. Both add migrations (protected): expect ESCALATE -> isolation review -> `gates.py` -> hand merge. 145 merges only after 146.

## Dependency rules for `new` lines
- SP5: #256/#257 after #255 (job 133). SP5-4 (#257) needs the lead to add appgen-web lines to protected `factory/checks.sh`.
- SP6: #271..#275 wait on #262 (SP5-9). SP7: #277 after #255; #278 after #277; others per body. SP8: #286 after #270+#271; #288 after #287+#279-#281; #290/#292/#294 per body (and carry the preconditions commented on them).

## Traps learned
- `gates.py` full suites in parallel cause flakes; run one at a time (~36 min each).
- Builders often stall at the end ("no progress for 600s") after pushing: check `build.md` and the branch before respawning.
- Reviewer-ui sometimes replies without writing `review-ui.md`; `job advance` then refuses: ask it to write the file.
- A `cd` into a worktree in a Bash call moves the session cwd; use absolute paths.
- Earlier traps hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, DECISIONS conflicts on job branches).
