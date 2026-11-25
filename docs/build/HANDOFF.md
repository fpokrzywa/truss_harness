# Handoff — 2026-11-25, charter 7 factory run (lead session agt-trus-harness-2f), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running. Merged this session: batch 10 (PR #429, `2dc5007`, head `e2a31f1`, gates 18/18, 8836 passed, 13 skipped in 1671 s, CI run 45672838202 green on that head before the merge): #415 (job 202), #425 (job 204), S-069, B-4171. No migration, no hosted setup due. Main CI on `2dc5007` not yet seen: look at it first. No agent, gate or wait loop of the lead is running.

## Rulings
- Supervisor: **seth-supervisor-11** (took over 13:14). Next free ruling S-070 (for 194). Message the supervisor first. Never edit S-* rows. Any conflict other than DECISIONS.md goes to the supervisor as a diff. Poll CI with REST (`gh api repos/fpokrzywa/truss_harness/actions/runs?head_sha=...`).
- S-059 to S-061, S-065: 194 then 193, one at a time, each with its own ruling. A failure of test_r1_round5 or tests/runtime/test_worker.py on any head goes to the supervisor before a merge.
- Merge rule: 18/18 and green CI of the exact head (`--match-head-commit`, merge lock `python scripts/unit.py lock merge --unit <x>`), composition from the supervisor on the exact head; no rerun-and-merge on a red CI. autostart.ps1 shown as deleted: neither commit nor restore.
- Reviewer briefs: say "tests take their environment through the documented test setup, do not open .devdata yourself".
- #421 and #430 before any live validation (Docker, Palantir; SP8-11, #296).

## Exact next step
1. `factory/bin/job queue`, ListAgents, message the supervisor, main CI on `2dc5007`.
2. **194** (#292, PR #405, `56cb96c`, needs-human: approver ESCALATE by rule only; code, security, isolation review 3 APPROVED, gate PASS, CI green): branch `lead/batch-11` from origin/main, merge it (supervisor predicts DECISIONS.md as the only conflict; any other conflict hunk or source change needs a delta isolation review). Send the sync head. B-4163 wording only, as the three replacements in decision.md "For the human" item 2 give it (same or separate commit); leave the adapter docstring alone. No code change for findings 1-3 (they are #430, no factory label until 194 is merged). Full gates and green CI on exactly that head, demos on it: palantir, delivery, snowflake, docker, solution, release (send the last lines, gates, CI run, conflict files), then S-070, then the merge with the lead's B-4172 record row.
3. **193** (#290, PR #404) after 194: `factory/bin/job feedback 290` with its lead-feedback-draft.md plus items 6-9 of 194's feedback-1.md with 193's own DECISIONS ids, plus isolation review 2 finding 4 of 194 (193 must name Fabric explicitly in `toolchain_digest` (runner.py:222) and `_details` (runner.py:1027-1036)); a new builder; it reuses 194's #406 mechanism and 195's `_recheck`.
4. Then #421, #397, #406/#407 (close when 194 and 193 carry them; do not start separately), #430 (add the `factory` label after 194), then #295, #296. Concurrency 8.

## Jobs and issues
- New: #430. #419 (no label, needs a migration), #395 (owner's). #425 and #415 closed by batch 10.
- For #295: the targets screen shows blank cells for an unusable target row (194's #406): it needs a hint.
- 194 review notes recorded in its job folder: rounds 2 and 3 (`isolation-review-2.md`, `-3.md`).
- Next free lead decision id: **B-4172** (B-4170 unused).
- Worktrees `.worktrees/batch-10`, `batch-10-merge` and databases `shifde_194_iso`, `shifde_194_iso3`, `shifde_202_iso2` left by reviewers may be removed by hand.

## New this session
- Always `export PYTHONUTF8=1` before `factory/bin/gate` and `job`: without it a gate crashed on cp1252 and wrote no verdict.
- Reading the handoff, charter, skills and STATUS costs about 70k of the 200k: start the big agents early and use `cut` on STATUS.md.

## Earlier: learned by session agt-trus-harness-00
- `job advance` from review with every review PASS goes only to approve: for extra tests, run the approver (tell it the facts, not the decision), needs-human, `job feedback`.
- An isolation reviewer found what gate, code review and the supervisor's reading missed (202): run it on every worker or executor job.
- A shell `cd` moved the session's directory to `.worktrees/batch-9` for good, and agents then started there: use `git -C` and absolute paths (`python /s/.../scripts/x.py` works).
- Do not start a factory gate before the builder's completion notification (done once for 198; passed).
- Scratch: `.worktrees/_gate-logs/200-iso-scratch` (220 MB, safe to delete), database `shifde_200_iso`, worktrees `195-iso`, `198-iso`, `199-base`, `199-head`, `199-load`, `batch-8`, `batch-9`, `batch-9-merge` and their databases may be removed by hand.

## Earlier traps
- `gates.py --name X` checks out the ref into `.worktrees/X` and detaches it: never use the name of your own batch worktree (it took over `.worktrees/batch-7`); use `<batch>-merge`.
- The factory gate takes about 5 minutes; the full gates 36 minutes with pytest 1699 s (3 builders alongside).
- `union_merge.sh` takes the branch name without `origin/`. `saverev.py <agent id> <out> <title>` saves the LAST long hand-back of an agent (a second hand-back overwrites the first file if you reuse the name).
- A reviewer asked a precise follow-up question reversed its verdict (195): ask reviewers about the two S-060 questions (token cache, stored rows) on every executor job.

## Helper scripts (gitignored, `.worktrees/_gate-logs/`)
- `union_merge.sh <worktree> <remote-branch> <message>`: merge with DECISIONS.md by union; stops on any other conflict.
- `saverev.py <agent id> <out.md> <title>`: saves a reviewer's SubagentHandback as a review file.

## Dependencies (job queue does not know them)
#295, #296 last. #294 also takes 173 review 3 finding 2 (render must dispatch by kind with a refusing default, commented on #294), the `DockerBindings.workspace_host` claim check; #292 takes nested Palantir suffixes and `frontend_origin` before any fetch; #295 takes the five kinds in `TargetsView.tsx:52-61`. `select .e5from secrets` stays open for the live test: no live Snowflake app before it is answered (S-043, S-049). When SP8 finishes, run its done-when demo and record it in STATUS.md.

## Leftovers
- `.worktrees/batch-6` (branch `lead/batch-6`, PR #399 closed, superseded) and the half-removed folders `.worktrees/batch-3-merge`, `batch-4-merge`, `batch-5-merge` may be removed by hand (long paths; git no longer lists them).
- `lead-feedback-draft.md` in 191's job folder was sent as feedback-1. `.worktrees/_gate-logs/uvplug.py` is a one-off reproduction plugin.
- `factory/bin/job queue` took more than 2 minutes. A builder of SP8 size took 100 minutes and 500k tokens: split further slices.
- Demos from a worktree: `uv run --frozen --project backend python scripts/<demo>.py --fake` (plain `python` is the main checkout's venv).
- Branches `lead/batch-4`, `lead/batch-5` and worktrees `.worktrees/batch-3`, `batch-4`, `batch-4-merge`, `batch-5`, `batch-5-merge` are merged and may be removed. Scratch databases `shifde_184_iso3`, `shifde_364_iso1` were left by reviewers.
- The Agent tool's `isolation: worktree` puts worktrees in `.claude/worktrees/`, which is not gitignored and makes main dirty: do not use it; create `.worktrees/<name>` by hand instead.
- `gates.py ... | tail` hides its exit code: read its lines.

## Traps learned
- Do not `cd` into a job worktree: while a shell's directory is inside it the factory cannot remove it, and a bare `git` in the emptied folder acts on the main checkout. Use `git -C <path>` and absolute script paths.
- `gates.py` output is buffered when redirected: run it with `python -u`, or read `.worktrees/_gate-logs/<name>/`.
- The gate inside `job merge` writes `round-N/merge-checks.md`, not `checks.md`.
- CI runs on main showed `cancelled` when a newer main push followed.
- CI `backend` took 25 to 38 minutes; every sync that moves the head restarts it. Push docs to main before syncing a branch.
- Wait for the builder's completion notification before `factory/bin/gate` (its stop hook runs `checks.sh` in the same worktree).
- TaskStop on a gate leaves its children: `taskkill /F /T /PID` only the tree of your own command, after re-checking its command line.
- One full gate run at a time (about 27 minutes); one factory gate alongside it was fine. Run a long gate as the lead's own background Bash task.
- Load flakes (`[Errno 10048]`, WinError 10055): a targeted rerun passes (B-3984). Full gates catch stale pinned expectations `checks.sh` skips (`tests/authz/test_catalog.py`, `tests/app/test_routes.py`, `tests/identity/test_schema.py` heads).
- `find .` from the repo root times out. A wait loop that greps process command lines matches itself. `uv run alembic heads` does not work from `backend/`.
- If a factory gate reports `'eslint' is not recognized`, run `pnpm install --frozen-lockfile` in that worktree's `frontend` and rerun it.
- Earlier traps hold: PYTHONUTF8=1, UV_LINK_MODE=copy, unset VIRTUAL_ENV, full job ids only, `factory/bin/job` run directly, worktree scripts via `uv run --frozen --project backend python ...`, DECISIONS id ranges per builder, isolation-reviewer is read-only (the lead saves its review), approver escalates any new migration by design. Startup reads cost about 90k tokens: use `cut` on STATUS.md lines and `python scripts/context_size.py` early.
- Read an agent's file before advancing: one spec-writer reported done without having written `spec.md`. Run `job advance` first and spawn the next agent only after it succeeds, not in the same batch.
- `job advance <id> build` refuses when every routed review passed, even if an isolation review found something. The route is approve, approver ESCALATE, needs-human, `job feedback`; point the approver at the review file.
- The "Wait for builder's completion notification before a gate" trap cost one overlap (183); a builder can also stall after READY (177): check the worktree is clean and equal to origin, then gate.
- A factory gate ran 4 to 17 minutes with a full gate and reviewers alongside. `pytest` logs in `_gate-logs` appear only when the suite ends (24 minutes).
- Isolation reviews are saved from the agent's `SubagentHandback` call in `~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl`.
- A builder can hand back its report and then be reported `failed: stalled`, with its own background shell still running (176, 184): check the worktree is clean, equal to origin, and that a mutation it described is reverted before the gate.
- A reviewer may return its review as a message and not write `review-<r>.md`: resume it (SendMessage by its id) to write the file.
- `scan-secrets.py` lists about 70 test-fixture lines: pipe it to `tail -1`.
- Agents' review saves: read the `SubagentHandback` input in the agent's `.jsonl` with a small script (one exists at `/tmp/saverev.py`, may be gone; arguments: agent id, output path, title).
- A shell `cd` into a worktree made the session's working directory that worktree for good: run the main checkout's scripts by absolute path (`/s/.../factory/bin/job` works from anywhere) and use `git -C`. `.worktrees/batch-3` (branch `lead/batch-3`, merged) is left over.
- Batch merge that worked: branch `lead/batch-<n>` from origin/main, `git merge --no-ff` each job branch, union merge of DECISIONS.md only (`git merge-file --union`), one PR with `Closes #n` lines, `gates.py` and CI on its head, `gh pr merge --merge --match-head-commit`: GitHub then marks the job PRs merged and `job queue` moves the jobs.
- `S:/tmp/HANDOFF.draft.md` is an older session's stray draft: leave the folder alone.

- A builder that is READY keeps its stop hook running: a factory gate started before the completion notification failed on a mypy INTERNAL ERROR (192); rerun PASS. Gate only after the notification.
- `gates.py` on 92e907a: pytest 1691 s with 3 builders alongside; 18/18.
