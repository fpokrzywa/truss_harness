# Handoff — 2026-11-15, end of session 5 (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Session 5 handed off at about 250k lead tokens (`scripts/context_size.py`), between units, with no agent running and no merge half done. Governance part 3 with its gates would have crossed the 300k line.

## Finished in session 5

All merged to `main` and pushed; main is at the commit that carries this handoff.

| Unit | Merged | Review |
|---|---|---|
| models_admin part 1 (data layer, `models_admin_0001`) | `4b08a71` | review 1 APPROVED, 8 follow-ups (closed or decided in 2a/2b) |
| admin part 2 (service accounts, API keys, platform group-binding route) | `dfa2f0b` | review 3 APPROVED, follow-ups closed in 3b |
| models_admin part 2a (providers, models, SHI switch, `models_admin_0002`) | `ba349a6` | review 2 APPROVED |
| tenancy fix B-1020a (client-list cursor read after rollback: 500 on a non-last page) | `64a5f5c` (lead) | read in admin review 4 |
| admin part 3a (IdP admin, `identity_0006`, IdP-sync audit) | `c192ecb` | review 4 APPROVED |
| models_admin part 2b (allowlists, routes, invoke, SHI call-time gate, `models_admin_0003`) | `a543c0a` | review 3 APPROVED. **models_admin is done**; plan Review Focus 5 pinned in `tests/models_admin/test_shi_gate.py` |
| admin part 3b (secret and audit routes, `secrets_0002`) | `9d9f100` | review 5 APPROVED (schema test confirmed not loosened). **admin is done** |
| governance part 1 (data layer, `governance_0001`, pricing, rate-card routes) | `cc6be1a` | review 1 APPROVED |
| governance part 2 (usage, budgets, quotas, exports; no migration) | `fb9f9f0` | none needed (not critical, no migration) |

Last clean-checkout gate run: `gates.py --ref governance --workers 6 --no-frontend` at `eab1f68` (main's code): 8/8, pytest 1140 passed in 295.2 s. Front end not re-run this session (no front-end change since `4bcc1fb`).

## The exact next step

1. **governance part 3** (sonnet, `module-builder`). Reuse branch `governance` (worktree `.worktrees/governance` exists, at main's code; run `git merge --ff-only main` first). Scope: SETH `governance/service.py` from `list_policies` (line 1347) to the end (policies, decisions, overview, retention get/put/preview, performance, `health_details`) and their routes; `shifde_ops_stats(int)` needs `jobs`/`runs` (runtime), so carry it. Close governance review 1 item 2 (`rate-cards/resolve` for shared SHI models; carry_forward row "governance (part 2/3)"). Decisions B-1821 on. If it adds a migration, isolation review. Then STATUS: governance done.
2. **Ledger guard fix, must land before runtime** (carry_forward row "ledger (follow-up, must fix before runtime)"; `docs/build/reviews/governance-isolation-review-1.md` item 1). Opus `module-builder-critical` (SECURITY DEFINER change), small: new ledger migration, the test run in the wrong tenant's context, isolation review. Can run in parallel with governance part 3.
3. **Then, in this order** (unchanged from session 4):
   1. `build` (sonnet, 8881 lines: split into about 4)
   2. `ontology` (sonnet; candidate for the charter §10 haiku trial if mostly a copy)
   3. `runtime` (opus, 8806 lines: split; C1 confirmed by test before acceptance; remove governance's `to_regclass` guards, B-1812; budget alert insert conflict target with the budget's own tenant, B-607; runtime halves of Items 9, 11, 15)
   4. `app`/`cli`/`main` (mount every router; deferred SETH tests in `carry_forward.md`; close identity review 4 item 5 (B-1625) before OIDC is mounted)
   5. the two-org proof (local cluster, S-002)
   6. the remaining front-end areas (B-1107: after `app`)
   7. update STATUS "Known defects"
   8. the factory install (charter §6)

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local and origin) | Up to date with this handoff commit |
| `governance` (origin) | At `eab1f68`, merged into main; reuse for part 3 (`.worktrees/governance`, `.devdata/dev.env` written, db `shifde_governance`) |
| `admin`, `models_admin` (origin) | Merged; done. Worktrees unregistered. The `.worktrees/admin` directory is left on disk (Windows "Permission denied"); delete by hand |
| `governance-1` (local only) | Pointer to `cc6be1a` used for gating; `git branch -D governance-1` |
| `.worktrees/verify`, `verify2`, `verify3` | Detached checkouts for `gates.py --name` |
| Leftovers | `.worktrees/fe-areas-1`, `.worktrees/secrets`, `.worktrees/skeleton`, `.worktrees/_scratch_admin`, `.worktrees/usage.txt`, `S:/tmp_identity_scratch`, `S:/tmp_identity_tools`, `S:/Development_2026/scratch_models_admin`: delete by hand |
| `observe/` (untracked, main checkout) | Not this build's; leave it (listed in `.git/info/exclude`) |
| Local cluster | PostgreSQL 17 on 127.0.0.1:55450, running. Reviewer scratch databases `shifde_tmp_review_*` are harmless |
| Background agents | None running |

## Learned, not recorded elsewhere

- **Overlap pattern that worked:** gate and isolation-review a fixed commit (`git branch -f <unit>-N <sha>`, `gates.py --ref <unit>-N --name verify2`) while the next part builds on the same branch; send the review's follow-ups to that builder with SendMessage. Tell reviewers to read files with `git show <sha>:<path>` and run tests in their own scratch worktree.
- **After merging a branch into main with a real merge commit, gate main before pushing.** Admin part 2 and models_admin 2a were combined untested once, and that run exposed B-1020a.
- **B-1020a lesson:** a "flake" that recurs in the same test is a bug until shown otherwise. `ObjectDeletedError` after `session.rollback()` on an RLS-scoped object means an expired attribute was reloaded outside the context that made the row visible.
- **Docs merge conflicts** (`DECISIONS.md`, `carry_forward.md`): union, but where the branch narrowed or closed a row, take the branch's row and drop main's; check for duplicates with a `Counter` over table lines.
- **Git Bash here:** `/tmp` is not writable (use `.worktrees/`, gitignored), and long multi-heredoc commands can fail to parse; write files with the editor tool instead.
- **Spend:** the usage report shows $285.17 estimated at list price, up from $222.35 at the start of session 5. It also lists `claude-fable-5-2` at $19.50 (80 requests). No agent this session used Fable. The charter forbids Fable for agents, so if that figure grows, check the agent files in `.claude/agents/` and any `model` overrides.
- Opus builders again ran 195-230k tokens per part even at 350-550 SETH lines; keep parts that size or smaller.
