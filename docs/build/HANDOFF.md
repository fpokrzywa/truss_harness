# Handoff — 2026-11-23 ~21:50 EST, charter 7 factory run (lead session 282359fa)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. Lead context ~180k at handoff. No agent running, no gate run.

## Finished this session
- Merged 147 (#256, PR #314), 148 (#257, PR #313), 150 (#318, PR #320), 149 (#277, PR #317), 145 (#270, PR #311), 152 (#319, PR #321), 154 (#260, PR #326, `job merge`). Gate counts in the STATUS entry.
- 150 (home page redirect, supervisor ruling S-038) is merged; the supervisor session was not identified in `ListAgents`, so tell it if it asks.
- Lead rulings B-3966 (job 155 findings) and B-3967 (job 151 deviation). Follow-ups filed: #324, #325, #327, #329.
- **Next free lead decision id: B-3968.** (B-3960 is job 153's builder row on its branch; B-3963-B-3965 were offered to job 154's builder, which used none.)

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`; start every `resume`/`new` line (check "Depends on" in each issue body before `job create`; `job queue` does not know dependencies).
2. **155** (#278 SP7-2, PR #328) at stage gate: `factory/bin/gate 155-sp7-2-assist-skeleton`. It adds protected migration `assist_0001`, so the gate may fail on the protected path by design (B-2530): then the lead runs reviews (code, security; plus `isolation-reviewer`, new client-owned tables), then the full `scripts/gates.py` on the synced head, then `gh pr merge`.
3. **151** (#315, PR #322) needs-human: sync with main (DECISIONS: renumber its B-3952 row to the next free id; 152's B-3952 is on main), push, `scripts/gates.py --ref <sha> --name 151-merge`, `gh pr merge 322 --merge --match-head-commit <sha>`.
4. **153** (#258 SP5-5, PR #323) needs-human (new dev deps flask/databricks-sdk; generated app calls Databricks with no access check of its own, design 6.4). Run `isolation-reviewer` (marked C), sync, full gates, merge. #327 (body cap) must merge before #259 deploys the app.
5. **143** (#285, PR #308) and **140** (#293, PR #303) needs-human, isolation APPROVED (docs/build/reviews): sync, full gates, merge.
Startable now: #316 (needs #256, merged), #324 (after #315), #325 (#319 merged), #327 (after #258), #329 (after #260), #259 (after #258), #261 (after #259+#260).

## Open jobs (stage)
140 needs-human · 143 needs-human · 151 needs-human · 153 needs-human · 155 gate.

## Traps learned
- `scripts/gates.py` from main now has 18 gates (appgen-web). A branch that changes `gates.py` must run its own copy: `python .worktrees/<job>/scripts/gates.py --ref <sha> ...`.
- `gates.py` output is buffered until it exits; progress is the files in `.worktrees/_gate-logs/<name>/` (pytest's log appears only when the suite ends).
- `job merge` fails fast on a conflict with main and moves the job to needs-human; most conflicts are DECISIONS.md rows: keep both sides, main first.
- Don't `git reset --hard` in job worktrees (auto mode refuses it, and it is not needed: check `git status` and the pushed head instead).
- Builders sometimes keep running after reporting READY; check build.md's first line and the pushed head, then stop them.
- Isolation-reviewer is read-only and cannot write its file: the lead writes it from the report, marked as such.
- Earlier traps hold (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, full job ids only, never two gate runs at once).
