# Handoff — 2026-11-23 ~11:45 UTC, charter 7 factory run (lead session eb9319b4)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit. Cluster on 55450 running. Main CI green again on `9666582` (bandit fix, job 129). No gate, merge or agent running. Open PRs: none.

## Finished this session
- Merged: 102 CM-7 (#165, PR #246, hand merge after DECISIONS-only conflict, B-3911), 127 (#238, PR #243, B-3912), 125 (#231, PR #244, r2), 126 (#235, PR #247, r2 code re-sync), 128 (#241, PR #248), 103 CM-8 (#166, PR #249: connection management done-when met against the fakes), lead migration `runtime_0007` (fast-forward `bf156e8`, B-3914), 119 (#224, PR #250), 121 (#218, PR #236, hand merge after `gates.py` 13/13, B-3914), 129 (#251, PR #252, main CI bandit B608, B-3915), 087 (#144, PR #200, r3, `gates.py --name 087-merge` 12/13 with only two WinError 10055 errors in TestClient setup; both files 203 passed at low parallelism; then `job merge` at `11ecf7e`, B-3913).
- Isolation review of PR #236 recorded: `docs/build/reviews/121-isolation-review.md`.
- **Next free lead id: B-3916.** Builder ids used up to B-3311; give new builders B-3321+.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`: start the `new` line for #253 (supervisor filed: extend the WinError 10055 retry to TestClient event-loop setup, `tests/_support/fullapp.py`).
2. Open factory issues besides #253: #174 (retention delete guard; needs lead migration on protected paths, items 1, 3, 4) and #147 part 2 (API key creators and trigger). Both need lead work, not a plain factory run.
3. Lead work on protected paths still open: B-3081/B-3905 schema-test move onto `shifde.cli.catalog_rules`.
4. Sub-projects 5 to 8 have no design yet (STATUS "Sub-projects 2 to 8" rows say "not written"). Per charter 7, the next lead writes each short design from the main design section 6, settles it, and cuts it into `factory` issues. SP4 row also still reads "in progress"; refresh it against issues #123-#131 (all merged?) before closing it.

## Traps learned this session
- `job merge` deletes the job worktree at the end; if your shell's cwd is inside it you get `PermissionError: [WinError 32]` after a successful merge. `cd` to the main checkout first. Harmless but noisy.
- Almost every `job merge` hits a `docs/build/DECISIONS.md` append conflict. Pre-sync: in the job worktree `git merge origin/main`; if the only conflict is DECISIONS.md, keep both sides (main's rows, then the branch's), commit, push, then `job merge` (sync is then a no-op). If code conflicts, abort and send the job back with `job feedback` (re-sync round), never resolve code as lead.
- A job at needs-human after a failed merge cannot be `job merge`d again; finish by hand: sync, `factory/checks.sh` in the worktree, `gh pr merge --match-head-commit <sha>`, DECISIONS row.
- `gates.py`/`checks.sh` do not run bandit; only CI does. Check `gh run list --branch main` after merges.
- Running scripts tests in a lead worktree needs `uv sync` and `factory/bin/jobdb ensure` there first, or procctl tests fail with the API exiting 1.
- WinError 10055 now also appears in TestClient's asyncio socketpair (#253); rerun the failing files at `-n 2` before calling a failure real. Earlier traps (PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, edit docs in binary mode to keep LF) still hold.
