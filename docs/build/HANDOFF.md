# Handoff — 2026-11-25 22:20 EST, charter 7 factory run (lead session agt-trus-harness-f4), context limit

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest progress entry and sub-projects table), then this file.

**State:** cluster running. Merged this session: batch 15 (PR #448, `0418229`, job 208 #421, S-074; 18/18 on the rerun, CI green, docker and delivery demos passed). Main is `77320ba` (STATUS). Nothing of the lead is running: no gate, no builder, no reviewer.

## Rulings
- Supervisor: **seth-supervisor-14** rules; message it first (it asked to be told that it rules). Next ruling id S-075 (reserved for #397). Never edit S-* rows. Any conflict other than DECISIONS.md goes to the supervisor as a diff.
- Merge rule: 18/18 from one full `gates.py` run of the exact head (a targeted rerun does not count), green CI of that head, the demos the ruling names; `gh pr merge <n> --merge --match-head-commit <FULL 40-char sha>` (a short or built-up sha fails with "Head branch was modified"); lock `python scripts/unit.py lock merge --unit <x>` / `unlock merge --unit <x>`. Approver APPROVE jobs also go through a lead batch with a composition check (209 did).
- Any test failure on any head goes to the supervisor before a merge (batch 15's WinError 5 suite-lock error: supervisor ruled a full rerun; filed #449).

## Exact next step: batch 16 (job 209, #441, PR #453)
Head `b841657` on `lead/batch-16` (worktree `.worktrees/batch-16`), composition checked by the supervisor. It merges on 18/18 of `gates.py --ref b841657 --name batch-16-merge` and green CI of `b841657`; no demos (tests only). Run the gates as a background Bash task: `python -u scripts/gates.py --ref b841657 --name batch-16-merge > .worktrees/_gate-logs/batch-16-gates.out 2>&1`. Then merge, send the supervisor the merge sha, and add a STATUS line.

## Then: #397 lead migration (S-075)
- `lead/397-migration` at `d5f6d19` (worktree `.worktrees/lead-397-migration`): `delivery_0010_palantir_version_binding.py` (GRANT UPDATE (bindings) only; BEFORE UPDATE OF bindings guard, SECURITY INVOKER, search_path pg_catalog, pg_temp; 42501 unless only a canonical-UUID `version_id` changes on a palantir target; owner-role early exit, accepted by the supervisor with pins), `tests/delivery/test_palantir_version_binding_db.py`, `tests/identity/test_schema.py` head pin, DECISIONS B-4192. The isolation review of `23b9775` is APPROVED (`.worktrees/_gate-logs/397-migration-isolation-review-1.md`). The delta `23b9775..d5f6d19` adds the pins the supervisor asked for (not a member of the owner role; no SECURITY DEFINER function mentions delivery_targets; the trigger list; prosecdef/proconfig), each shown to fail under a mutation (builder's report).
- Next: full `gates.py --ref d5f6d19` (after batch 16's gates: one full run at a time), CI on it (open a PR for CI), then send the supervisor the head, the gate result and the delta `git diff 23b9775 d5f6d19` for S-075. After merge: the hosted database setup is re-run (owner's step, see S-054), and job 210 (#397, parked at **intake**, REJECT written because of the protected path) is redone: re-run intake telling it the migration is merged, or ask the supervisor how to move it. The route spec must check scope (workspace, project), status (active, not archived), the target administrator, the row version and the platform (supervisor).

## After that
- **206** (#295, SP8-10 front end): stage `build`, round 1, **no builder spawned**. Spawn `builder` named `builder-206-sp8-10-front-end`, port 7160; it first merges main and checks the Fabric names; the front end lists nine MCP tools and the API has twelve.
- Then **#434** (Palantir clients.py), **#296** last (SP8-11 live tests, runbooks). New factory issues: #449 (suite lock WinError 5), #451 (runtime: deadline-cut tool call should end timed_out; isolation review), #452 (sibling killed-worker tests). #447 (N4 Docker daemon re-check before compose up; no label, due before SP8-11). #419 (no label, migration), #395 (owner's).
- Lead decision ids: next free **B-4194** (B-4188, B-4189 unused; B-4192 is the 397 migration's).

## New this session
- `cd` inside a Bash command still moved the session directory twice (once into `.worktrees/_gate-logs`). Use subshells `( cd X && ... )` or absolute paths.
- A gate log with a reused `--name` is overwritten by the next run: #441's original failure log was lost. Use a new name for each rerun (`batch-15-merge2`).
- Intake REJECTs any issue needing a migration (protected path); the route is lead hand work by a `module-builder-critical` on a `lead/` branch, isolation review with mutations, full gates, supervisor ruling (B-4089, S-054, #397).
- Scratch: worktrees `batch-15`, `batch-15-merge`, `batch-15-merge2` may be removed by hand.

## Earlier: learned by session agt-trus-harness-98
- `cd` in a Bash call into a worktree or job folder moves the session's directory: use absolute paths in every command (it happened three times).
- Demos from the main checkout on a gate worktree worked: `uv run --frozen --project $W/backend python $W/scripts/<demo>.py --fake`.
- Scratch: worktrees `batch-13`, `batch-13-merge`, `batch-14`, `batch-14-merge` may be removed by hand; `.worktrees/_gate-logs/207-r1-gate1-checks.log` is the kept flake log.

## Earlier: learned by session agt-trus-harness-da
- `lead-feedback-final.md` in 193's job folder was sent as feedback-1. An isolation reviewer found LOW findings in 205 and 193 that gate, code and security review missed: run it on every executor or adapter job.
- The code review of 205 was thin (4 tool calls): check its claims on the diff.
- Reviewers' scratch: databases `shifde_iso205` and 193's reviewer's, worktrees `batch-11`, `batch-11-merge`, `batch-12`, `batch-12-merge` and older ones may be removed by hand.
- Do not `cd` into a job or round folder: the shell keeps it as the session's directory.

## Earlier in this chain
- Always `export PYTHONUTF8=1` before `factory/bin/gate` and `job`. Startup reads cost about 70k to 100k of the 200k: start the big agents early and use `cut` on STATUS.md.

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
