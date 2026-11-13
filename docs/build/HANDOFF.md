# Handoff — 2026-11-13, end of session 1 (lead)

Session 1 paused here at the request of the supervising session (`agt-trus-harness-0b`), which says the owner asked it to run the build from now on in sessions it starts. Read `docs/build/STATUS.md`, then this file.

## Finished

- Sub-project 1 plan: `docs/superpowers/plans/2026-11-13-subproject-1-foundation-and-seth-port.md`.
- Control files: `CLAUDE.md`, `docs/build/STATUS.md`, `docs/build/DECISIONS.md` (B-001…B-010 on main), `docs/build/prices.json`, `.gitattributes` (LF).
- Agent definitions in `.claude/agents/` (scout, module-builder, module-builder-critical, frontend-builder, verifier, isolation-reviewer). They load only at session start: a new session can use them by `subagent_type`. In session 1 they ran as `general-purpose` with the role's model and a `(role)` tag in the description (B-008).
- Usage report: `scripts/usage_report.py` + tests (`python -m unittest discover -s scripts/tests`).
- Audit findings mapped to modules: `docs/build/findings.md` (2 critical, 36 major, 30 minor; 19 majors are known defects for sub-project 1).
- Front-end shell: merged to main (`6f60b00`), verified. File-to-area map for later front-end ports: `docs/build/frontend-areas.md`.
- Hosted database check: held, waiting on the owner (no TLS, PostgreSQL 16.13). Nothing created there (B-009).

## The exact next step

**Skeleton fix round 1.** Branch `skeleton` (`5acc54d`, pushed to origin, not merged) passed the verifier but the isolation review BLOCKED it. Send `docs/build/reviews/skeleton-isolation-review-1.md` to a `module-builder-critical` (opus) working in `.worktrees/skeleton`:

- Fix blocking items 1–4: the RLS test must reject constant-true or non-tenant permissive policies and require WITH CHECK; it must cover views, materialized views and foreign tables; the role check must refuse REPLICATION and CREATEROLE; it must also refuse preset `app.*` settings, with a pool checkout assertion.
- Also fix the follow-ups the review assigns to the "skeleton fix round": 5 (if cheap), 7, 8, 11 and 13.
- Each fix needs a probe test that fails first.

Then:

1. Re-run the verifier from a clean checkout.
2. Re-run the isolation reviewer with round 1 attached.
3. Merge to main and push.
4. Merge `docs/build/frontend-ci-job.yml` into `.github/workflows/ci.yml`. The skeleton's `ci.yml` has a conditional frontend job; replace or confirm it, and delete the yml doc.
5. Update STATUS, regenerate the usage report, commit, push.

Then start module `core` (plan Task 5), followed by the layer order in the plan.

**Merge conflict to expect:** the skeleton branch was cut before main gained `fe-shell`, B-009 and B-010. `docs/build/DECISIONS.md` and `CLAUDE.md` will conflict. Keep both sides: main's B-001…B-010 rows, and the branch's B-101…B-111 rows and local-command edits.

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local and origin) | Up to date with this handoff commit. |
| `skeleton` (local and origin) | `5acc54d`. Verifier PASS; isolation review BLOCKED; not merged. |
| `.worktrees/skeleton` | The builder's worktree on `skeleton`. Clean. Reuse it for the fix round. Its app database `shifde_skeleton` is on the local cluster. |
| Local cluster | PostgreSQL 17 on 127.0.0.1:55450, data `.devdata/pg`, secrets `.devdata/cluster.env` (never print). Running. Start and stop it with `python scripts/dev_db.py start|stop|status`; `env` writes a worktree's `.devdata/dev.env`. |
| Background agents | None running. |

## Learned, not recorded elsewhere

- **Subagent report files:** subagents here may not write report files (one was refused with "Subagents should return findings as text"). Builders take their own inventory (B-010). The lead saves reports that matter, as with `findings.md` and the review file.
- **Removing worktrees on Windows:** `git worktree remove --force` can leave the directory behind because of `node_modules` long paths. Empty it with `robocopy <empty dir> <dir> /MIR`, then delete it. This can take over 2 minutes, so run it in the background.
- **Test parallelism:** this machine has 96 cores, so `pytest -n auto` starts 96 workers. That took 90–130 s for 36 tests, versus about 35 s with `-n 8`. Consider a fixed `-n` in the gate commands once the suite grows. Measure before changing; the design's target is under 20 minutes for the full suite.
- **Skeleton decisions:** B-101…B-111 on the `skeleton` branch say:
  - SETH's tables live in schema `shifde`, not `public`.
  - The tenant context functions are SETH's (`shifde.current_tenant_id()`, `app.tenant_id`), not the plan's `shifde_current_org()`/`app.org_id`. The tenancy port must not re-create them.
  - `setup_tokens` has RLS but no policy in SETH. Its port needs an explicit deny policy, logged in `deliberate_changes.yaml`, or a reviewed global-table entry.
  - The schema comparison covers tables and triggers, not SQL functions. The reviewer's follow-up 8 adds a SECURITY DEFINER test.
  - `shifde_owner` keeps BYPASSRLS, as in SETH; it is only for migrations and maintenance.
- **Hosted database:** it is a shared server holding about 25 other databases. The role in `DATABASE_URL` is a superuser, and TLS is off. Never touch anything there but `truss_harness`, and only once TLS exists.
- **Supervisor request:** a supervisor session asked to add a charter section 10 and `scripts/context_size.py`. Session 1 did not add them: the charter is the owner's instruction, and charter §2 forbids committing files from `.devdata/`. The supervisor said it would commit them itself from its own copies after this push.
