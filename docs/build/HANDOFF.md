# Handoff — 2026-11-14, end of session 3 (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Session 3 handed off at about 280k lead tokens, between units, with no agent running and no merge half done. The next step is a fix round whose cost would have crossed 300k mid-unit (charter §10).

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

Identity decisions are B-901..B-927. Reviews are in `docs/build/reviews/`.

Main is at the commit carrying this handoff, with 808 backend tests. The last clean-checkout gate run of main's code was `gates.py --ref identity` at `20358ad`: 8/8, 808 passed. Later commits change docs only. The front end is unchanged since `c925793` (13/13 then).

Also recorded:
- S-002, an owner instruction relayed by the supervising session: use the local cluster for everything, including the two-org proof. The hosted database stays "waiting on owner input" but is deferred, not a blocker.

## The exact next step

**Identity part 3b, fix round 1.**
- Branch `identity` at `ccfe02a` (= main `091ce25` + part 3b, SHI-wide groups, B-928..B-935). It passed 8/8 gates (821 tests).
- Isolation review 5 BLOCKED it: `docs/build/reviews/identity-isolation-review-5.md`.
- **Blocking item 1:** remove `shifde.group_members` from `PRINCIPAL_TABLES` and from `test_reviewed_principal_tables`. Pin its `own_row` SELECT policy exactly in `REVIEWED_EXPRESSIONS`. Add the behaviour tests the review describes:
  - as `shifde_app` with no client set, and again with B's context: self-insert into a group bound in B is refused, and so is inserting another person into one's own group;
  - UPDATE and DELETE affect 0 rows.
- **Item 2, cheap, same round:** `shifde_sync_idp_groups` also refuses when `current_tenant_id() IS NOT NULL`, with a test.

How to run the round:
1. Brief a **fresh** `module-builder-critical` with the review path and the worktree `.worktrees/identity`. The part 3b builder ended at 262k, so do not resume it.
2. Run the gates.
3. Re-review the fix-round diff with a fresh `isolation-reviewer`.
4. Fast-forward main (if main has moved, merge and gate), push, and update STATUS.
5. Add review 5 items 3-5 to `carry_forward.md`:
   - admin/tenancy: audit group membership changes and group bindings into each client where the group is bound; fix the admin row that says "only if decided under isolation review";
   - tenancy: `shifde_active_group_ids` with `p_user = current_principal_id()`;
   - sub-project 3: `check_idp_group_scope` caller rights.
6. Then mark identity **done** in STATUS.

**Then, in the plan's layer order:**
1. `tenancy`: opus, split into about 2-3 parts (B-013). Its carry_forward rows are many; read them all. They include:
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
| `identity` (local, origin) | `ccfe02a` (part 3b, BLOCKED, not merged to main; pushed to the branch). Worktree `.worktrees/identity` is clean at `ccfe02a`, with database `shifde_identity` |
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
