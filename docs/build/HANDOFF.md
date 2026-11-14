# Handoff — 2026-11-14, end of session 4 (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Session 4 handed off at about 255k lead tokens (`scripts/context_size.py`), between units, with no agent running and no merge half done. The next unit (connections follow-ups, then admin part 2), with builder, gates and review, does not fit under the 300k line in charter §10.

## Finished in session 4

All units below are merged to `main` and pushed. Main is at the commit that carries this handoff. Flaky under load (passes alone): `tests/connections/test_api.py::test_connection_call_deadline_applies_to_direct_invokes`.

| Unit | Merged | Review |
|---|---|---|
| installation (minimal) | `34612b8` | none: no migration, not critical (B-1101..B-1106) |
| front-end areas 1: login, setup, platform settings | `4bcc1fb` | n/a (B-1107) |
| tenancy part 2a: service, `tenancy_0002` | `2c3b4b4` | review 2 APPROVED |
| tenancy fixtures: authz/secrets fixtures on real rows | `970cf60` | none: tests only (B-1201) |
| tenancy part 2b: routes, `tenancy_0003` | `b291be3` | review 3 APPROVED. **tenancy is done** |
| vendors | `ccb64c7` | review 1 APPROVED (B-1301..B-1304). **vendors is done** |
| admin part 1: IAM routes | `0071471` + fix `31f1b0b` | review 1 BLOCKED, then review 2 APPROVED (B-1401..B-1410, `tenancy_0004`) |
| connections part 1: data layer, `connections_0001` | `60074c3` | review 1 APPROVED (B-1501..B-1504) |
| connections part 2: service, routes, C2 | `04440d9` | review 2 APPROVED (B-1505..B-1510). **C2 confirmed by test. connections is done** |

The last clean-checkout gate run was `gates.py --ref connections --workers 6` at `8cd03b8`, which is main's code: 13/13 passed, pytest 1001 passed in 292.6 s, front end 0. Later commits change docs only.

## The exact next step

1. **Connections follow-up unit: done after the first handoff** (`a19c1f1` + `4ddce65`, merged; review 3 BLOCKED on an R+1 race variant, review 4 APPROVED; 1006 tests, 13/13 gates). The session was kept open by its stop hook, and context was still under 300k, so this unit ran after the first handoff. Start at step 2.
2. **`admin` part 2: service accounts and API keys.** Opus. Reuse branch `admin` from main, with SETH `admin/credentials.py` and the service-account and api-key routes of `api.py`.
   - Close the carry_forward rows naming admin for these routes: the `test_p2_credentials.py` remainder, `revoke_api_key`/`touch_api_key` mutators, and `created_by` = caller (B-921).
   - Possibly add a platform-admin route for binding a group into a client (new row).
3. **`admin` part 3: identity providers, secrets, audit routes.** Opus. SETH `admin/providers.py` plus the idp, secrets and audit routes.
   - Close the IdP rows (identity review 4 items 5/7, `test_p2_review` OIDC tests).
   - Close the secret routes (B-802/B-803, secrets review 1 items 1-2, `create_secret` error mapping) and the audit routes.
   - **Close the IdP-sync self-add row before `app` mounts OIDC** (admin review 2 item 1).
4. **`models_admin`.** Opus. Covers:
   - the platform-level SHI provider, off per client by default, with an audited switch shown to the owner and a gate at call time (plan Task 14, Review Focus 5);
   - FKs to `vendors`;
   - `require_active_vendor`;
   - Items 9, 11 and 15.
5. **Then, in this order:**
   1. `governance` (sonnet, 3532 lines: split)
   2. `build` (sonnet, 8881 lines: split into about 4)
   3. `ontology` (sonnet)
   4. `runtime` (opus, 8806 lines: split; C1 confirmed by test before acceptance)
   5. `app`/`cli`/`main` (mount every router; the deferred SETH tests listed in `carry_forward.md`)
   6. the two-org proof (local cluster, S-002)
   7. the remaining front-end areas
   8. update STATUS "Known defects" (filled in session 4) as modules close findings
   9. the factory install (charter §6)

**Front-end areas follow routes, not modules (B-1107).** Most SETH screens call `/api/v1/orgs/{org}/...` routes that only exist once admin, tenancy and the rest are mounted by `app`. Port the remaining front-end areas after `app`, or after each admin part, against routes that exist.

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local and origin) | Up to date with this handoff commit |
| `installation`, `fe-areas-1`, `tenancy`, `tenancy-fixtures`, `vendors`, `admin`, `connections` (origin) | Merged. Worktrees removed. `admin` can be reused for parts 2-3 (`git worktree add .worktrees/admin admin`, then `git merge main`) |
| `.worktrees/verify`, `verify2`, `verify3` | Detached checkouts used by `gates.py --name`. The lead uses `verify`; builders use `verify2` or `verify3` |
| `.worktrees/fe-areas-1`, `.worktrees/secrets`, `.worktrees/skeleton` | Leftover directories Windows would not delete (locked `node_modules`); git no longer tracks them. Delete them by hand |
| `observe/` (untracked, main checkout) | **Not created by this build.** It appeared at 08:37 and holds a usage dashboard (`server.py`, `dashboard.html`); it probably belongs to the supervising process. Do not commit or delete it. It is listed in the local-only `.git/info/exclude` (not committed), so `git status` is clean; remove that line if the owner wants it tracked |
| Local cluster | PostgreSQL 17 on 127.0.0.1:55450, running. Leftover `shifde_<worktree>`, `shifde_review` and `shifdet_*` databases are harmless |
| Scratch directories outside the repo | `S:/tmp_identity_scratch` and `S:/tmp_identity_tools` (from session 3). Delete them by hand |
| Background agents | None running |

## Learned, not recorded elsewhere

- **The cluster is shared, and load causes flakes.** With three suites running at once (two agents and the lead), one run failed a setup with `psycopg ConnectionTimeout`. Run `gates.py --workers 6` while agents run, and tell builders and reviewers "`pytest -n 6` at most". Two known timing flakes recurred under load, and each passed on rerun:
  - the cookie-expiry comparison in `tests/identity/test_cross_client.py` `_same_failure` (carry_forward identity row);
  - `tests/audit/test_service.py::test_concurrent_appends_keep_one_linear_chain`.

  Rerun a gate only when the failing test is one of these.
- **Critical builders run over the sizing.** Opus builders took 206-238k tokens per part, even with tight scope. Split critical modules into parts of about 500 source lines plus their tests. Units given a fix round always had their fix round run by a fresh agent, because the builder was over 200k.
- **Reviews.**
  - A resumed reviewer (SendMessage with the fix diff) costs about 25k and works well.
  - Reviewers are read-only and will not write the review file. The lead saves it to `docs/build/reviews/<unit>-isolation-review-<n>.md` from the report.
  - Admin part 1 was the only BLOCKED review this session: a platform admin joining a group before the group was bound.
- **Merging main into a branch.** `DECISIONS.md` and `carry_forward.md` conflict nearly every time. A union of both sides works, but:
  - drop the side's copy of a row the other side narrowed or closed. Example: the "vendors, audit (follow-up)" row was narrowed to "vendors (follow-up)" by admin;
  - drop rows a branch closed, such as "connections (part 2)".

  Check for duplicate rows afterwards with a `Counter` over table lines.
- **`gates.py` accepts `--workers N`.**
- **Wall time.** The full backend suite takes 225-297 s for 954-1001 tests with `-n 6` under load. That is far under the 20-minute target.
- **Spend.** The usage report shows $222.35 estimated at list price in total, up from $137.70 at the start of session 4.
