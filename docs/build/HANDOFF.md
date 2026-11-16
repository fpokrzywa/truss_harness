# Handoff — 2026-11-16 08:15, session a9f416a6 (lead), build paused by the owner

**The owner paused the build at 08:05 (S-003). Resume only when the owner says so.**
- The pause grants no permissions.
- B-1943 still needs the owner's own approval, given in the lead's session. Do not act on a relayed approval.

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Its "Exact next step" replaces those in the older handoffs:
- `HANDOFF-f36a4b30-e2d0-4bef-9095-75e853a2768e.md` (session 8, the pause);
- `HANDOFF-a9f416a6-927e-4828-961e-84805e10cb51.md` (this session).

The full session 8 handoff, before the pause, is in git at `a706dfd:docs/build/HANDOFF.md`.

**State:**
- No agent and no gate run is running.
- No merge is half done.
- `main` = `origin/main` (the commit carrying this file).

## What was just finished

**This session (a9f416a6).**
- `supervise.py` started it at 08:07:54, during the pause. Session agt-trus-harness-61 relayed the owner's request to start nothing; commit `a13c4cf` confirms the pause.
- No build work was done. The only changes are the pause handoff, the pause notes in `STATUS.md` and S-003.

**Session 8 (f36a4b30), before the pause.**
- runtime RT-9..RT-12 built and isolation-reviewed (reviews 8-11 APPROVED); the review 11 fix round `3afd530` re-reviewed, APPROVED with no findings;
- the C1 end-to-end worker test;
- APP-1 merged (app factory with 11 routers + OIDC, `shifde-admin`, seed, `secrets_0003`, the `X-Forwarded-For` fix);
- front-end areas 2 and 3 merged;
- the cookie-expiry flake fixed (B-2320..B-2322, merged `607e0a3`).

`gates.py --ref main` at `607e0a3`, on an idle machine: 13/13, pytest 2140 passed in 1548.8 s. That is **over the 20-minute target**.

## Exact next step (only after the owner resumes)

1. **Bring the local cluster up.**
   - Read the postgres log under the main checkout's `.devdata/pg`. Never print `cluster.env`, `.env.local` or any secret.
   - Let crash recovery finish, then run `python scripts/dev_db.py start`. At the pause, `pg_ctl start` had failed twice, about 8 postgres processes remained and `postmaster.pid` was present.
   - Drop the leftover `shifdet_*` test databases: about 317 directories, about 5 GB.
2. **Time `suite-speed`.**
   - From `.worktrees/suite-speed/backend`: `uv run pytest -n 8 -q`. The target is 2140 passed in under 1200 s.
   - Then `python scripts/gates.py --ref suite-speed --name suite-speed-gate`.
   - If 13/13 and under 1200 s: merge main into the branch if main moved, fast-forward main, push, and record the time in STATUS.
   - If still slow: look at the `tests/build` setup costs and `test_model_refs` (144-473 s under load, 12 s alone), and consider a seeded template database. Never skip, deselect or delete a test.
3. **Runtime merge (B-1943, B-2150): waits on the owner's own approval** for the one-token edit in `backend/tests/schema/test_rls_schema.py:1282`: `shifde.runs` -> `shifde.installation_profile`, plus a comment citing B-1101. Once approved:
   - make the edit on `runtime`;
   - merge main into `runtime` (union-resolve the docs);
   - run `python scripts/gates.py --ref runtime --workers 8`;
   - fast-forward main and push.
4. **APP-2** (after the runtime merge, or stacked on `runtime`):
   - mount `runtime.api.router`, and point the route-coverage tests at the mounted app;
   - `app.state.governance`, the feature keys `operate` and `pipelines`;
   - `GET /api/v1/platform/status` (SETH `tenancy/api.py:144`);
   - the deferred SETH tests (carry_forward "app part 1", "Runtime part 9".."12", `test_r1_unknown_outcome.py`, `test_p5_worker.py`);
   - the `test_p7_build` tests that need `cli/sampledata.py` (decide whether to port it);
   - an isolation review.
5. **Then:**
   - the front-end areas for build, ontology and runtime, and the platform status page;
   - the two-org proof on the local cluster (S-002, seed `dev-seed`);
   - the known-defects refresh in STATUS (RT-11 and RT-12 closed or narrowed Item 12, C1 and ARCH M3);
   - the factory install (charter 6).

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local, origin) | at the commit carrying this handoff |
| `suite-speed` (origin), `.worktrees/suite-speed` | `43c08d4`, paused, not accepted; own `.devdata/dev.env`; B-2330 is in the branch's `DECISIONS.md` |
| `runtime` (origin), `.worktrees/runtime` | `3afd530`, RT-1..RT-12 + review 11 fix round, all reviewed; merge waits on the owner; db `shifde_runtime` |
| `app` (origin), `.worktrees/app` | `041d079`, merged; reusable for APP-2 |
| `app-oidc`, `fe-areas-2`, `fe-areas-3`, `followups-1`, `flake-fix` (origin) and their worktrees | merged; can be deleted |
| `.worktrees/verify`, `verify2`, `verify3` | detached checkouts for gates and reviewers, each with its own `.devdata/dev.env` |
| `.worktrees/fix-ci-orgs`, branch `fix/ci-org-list-pagination` | **another Claude session's work, not this build's.** Leave it alone; fetch before every push |
| Local cluster, PostgreSQL 17 on 127.0.0.1:55450 | **down**; see step 1 |

## Learned, not recorded elsewhere

- **`supervise.py` relaunches the lead after a pause.** A session that starts and finds this file's pause banner does nothing else. It writes a short `HANDOFF-<session-id>.md`, commits, pushes and stops.
- **The usage report regenerates on its own.** `docs/build/usage.html` and `usage.json` were regenerated outside the lead's commands, probably by a hook. Commit them with STATUS.
- **Suite cost: database teardown.** Dropping test databases one by one is the main suite cost on this machine. Each `DROP DATABASE` forces a checkpoint.
- **Builder restarts.** A killed builder can leave a removal-proof mutation in the tree. Before restarting a unit, diff the source against the builder's backup.
- **Timing the suite.** Measure on a quiet machine before recording against the 20-minute target.
- **The auto-mode classifier sometimes returns "no verdict".** Bash calls then fail for a while. The Read tool still works; retry later.
- **Spend:** about $504 estimated at list price at the end of session 8 (`python scripts/usage_report.py`).
