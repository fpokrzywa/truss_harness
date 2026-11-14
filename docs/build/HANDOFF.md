# Handoff — 2026-11-14, end of session 3 (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Session 3 handed off at about 296-300k lead tokens (`scripts/context_size.py` read 295,826 after the identity merge), between units, with no agent running and no merge half done: the next unit (tenancy, several builder and review rounds) cannot fit in what is left before the 300k line of charter §10. (An earlier handoff at ~280k was premature and was resumed to finish identity.)

## Finished in session 3

| Unit | State | Merged | Review |
|---|---|---|---|
| authz follow-up 1 (`platform:admin` in a client role) | done | `a8da7f8`, fast-forwarded | B-406, `authz_0002`; review 2 APPROVED |
| audit | done | `857ef08` | B-701/B-702; review 1 BLOCKED, then review 2 APPROVED |
| secrets | done | `2b8ca1d` | B-801..B-806; review 1 APPROVED |
| core context hardening | done | `b34d083` | B-703; review 1 BLOCKED, then review 2 APPROVED |
| identity part 1 (data layer, 9 tables) | done | `99113c8` | review 1 APPROVED |
| identity part 2a (definers, membership guard, login service) | done | `00abeb7` | review 2 APPROVED |
| identity part 2b (API keys, dependencies, routes) | done | `d6d7b37` + fix `867b07d` | review 3 APPROVED |
| identity part 3a (OIDC) | done | `20358ad` | review 4 APPROVED |
| identity part 3b (SHI-wide groups) | done | `ccfe02a` + fix `b62d03b` | review 5 BLOCKED, then review 6 APPROVED |

Identity decisions are B-901..B-936. **`identity` is done.** Reviews are in `docs/build/reviews/`.

Main is at the commit carrying this handoff, with 867 backend tests (tenancy part 1 merged after the session resumed; gates 8/8 at `07556f2`). The last clean-checkout gate run of main's code was `gates.py --ref identity` at `b62d03b`: 8/8, 824 passed (an earlier run hit the `_same_failure` timing flake, recorded in `carry_forward.md`). Later commits change docs only. The front end is unchanged since `c925793` (13/13 then).

Also recorded:
- S-002, an owner instruction relayed by the supervising session: use the local cluster for everything, including the two-org proof. The hosted database stays "waiting on owner input" but is deferred, not a blocker.

## The exact next step

**`tenancy` part 2** (plan Task 10; opus `module-builder-critical`; worktree `.worktrees/tenancy`, branch `tenancy`, whose part 1 `07556f2` is merged to main as a fast-forward; database `shifde_tenancy`). Part 1 (data layer, deferred FKs, B-116 hardening, `shifde_my_memberships`) is merged and reviewed (`reviews/tenancy-isolation-review-1.md`). Part 2 = `tenancy/service.py`, `api.py`, `dependencies.py`, `schemas.py`: who may grant (platform admin or the client's owner; platform-admin self-add audited and shown to the owner), organization visibility from bindings incl. groups, the session pin check against current bindings and client status (Item 12), `shifde_revoke_user_sessions` scope, routes and route cross-client tests (non-vacuous: own object 200 first). Its brief must carry every carry_forward row still owned by tenancy (part 2), including the new tenancy review 1 row, and identity review 3 items 2-3 and 5. Part 1's builder used 288k tokens: split part 2 into service then routes.

**Then, in the plan's layer order:**
1. `tenancy` part 2 (the exact next step above). Its carry_forward rows are many; read them all. They include:
   - `tenants` FKs on every earlier table;
   - B-116 items 7/9/10;
   - the grant sweep;
   - `shifde_my_memberships`, and dropping the B-920 stub;
   - the client-owner grant path;
   - session-pin binding/status checks, which also covers Item 12;
   - `shifde_revoke_user_sessions` scope;
   - the identity review 3/4 test fixes;
   - workspace/project group bindings.
2. `installation` (minimal)
3. `vendors`
4. `connections`: opus; C2 is confirmed by test.
5. `admin` + `models_admin`: opus; the SHI provider switch.
6. `governance`
7. `build`: split.
8. `ontology`
9. `runtime`: opus; split; C1 is confirmed by test.
10. `app`/`cli`/`main`
11. The two-org proof (local cluster, S-002).
12. Front-end areas.
13. The factory install (charter §6).

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local, origin) | Up to date with this handoff commit |
| `identity` (local, origin) | `b62d03b`, merged to main (fast-forward). Worktree `.worktrees/identity` is clean and can be removed |
| `audit`, `authz-fix`, `secrets`, `core-ctx` (origin) | Merged. Worktrees removed. The `.worktrees/secrets` directory is left over (Windows refused to delete a locked file); git no longer tracks it |
| `.worktrees/verify`, `verify2`, `verify3` | Detached checkouts used by `gates.py` (`--name`). Reuse them |
| Scratch directories outside the repo | `S:/tmp_identity_scratch` and `S:/tmp_identity_tools` were left by the identity part 1 builder. Delete them by hand |
| Local cluster | PostgreSQL 17 on 127.0.0.1:55450, running. Leftover `shifde_<worktree>` databases are harmless |
| Background agents | None running |

## Learned, not recorded elsewhere

- **Agent cost:** module-builder-critical agents on identity parts ran 230-262k tokens each, even though each part is about 500 source lines. These are judgment-heavy isolation units. For tenancy, split smaller (for example data layer, then service, then routes and visibility). Give each part's brief the exact review items it must close.
- **Review rounds:**
  - Almost every critical unit's first review found something real. These included an admin flag left set after a failed write, a whole rollback restoring a savepoint's admin flag, a victim lockout, and a test registry that accepts self-grants.
  - Budget one fix round per critical unit.
  - A finished reviewer resumed with SendMessage for round 2 is cheap and works well. A small builder (under ~90k) resumed for its own fix round is also fine.
- **Merging main into a branch, or a branch into main:**
  - `DECISIONS.md`, `carry_forward.md` and `deliberate_changes.yaml` conflict every time.
  - The resolver `/tmp/resolve.py <file> ours|union` (it may be gone; it is a 10-line regex over conflict markers) keeps both sides.
  - Watch for duplicate rows, such as the B-701 row duplicated by a branch that carried an older copy.
  - When a branch moved rows to Done, drop them from the open table.
- **Line endings:** write docs from Python with `open(..., newline='')`. Git normalises to LF on commit; the CRLF warning is harmless.
- **`gates.py` names:** `--name verify3` works and creates the worktree. Builders use `verify2` or `verify3` while the lead uses `verify`.
- **Wall time:** full-suite pytest went from 64 s (529 tests) to about 120 s (808 tests) with `-n 8`, still far under the 20-minute target.
- **Spend:** the usage report shows est. $137.70 at list price in total. The Fable line ($19.50) is from session 1 or the supervisor, not from agents.
