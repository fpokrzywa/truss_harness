# Handoff — 2026-11-15, end of session 5 (lead)

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Session 5 handed off at about 300k lead tokens (`scripts/context_size.py`), right after accepting build part 1, with no agent running and no merge half done.

## Finished in session 5

All merged to `main` and pushed; main is at the commit that carries this handoff.

| Unit | Merged | Review |
|---|---|---|
| models_admin parts 1, 2a, 2b (`models_admin_0001..0003`, SHI provider switch and call-time gate) | `4b08a71`, `ba349a6`, `a543c0a` | reviews 1-3 APPROVED. **models_admin is done**; plan Review Focus 5 pinned in `tests/models_admin/test_shi_gate.py` |
| admin parts 2, 3a, 3b (service accounts and keys; IdPs, `identity_0006`; secrets and audit routes, `secrets_0002`) | `dfa2f0b`, `c192ecb`, `9d9f100` | reviews 3-5 APPROVED. **admin is done** |
| tenancy fix B-1020a (client-list cursor read after rollback) | `64a5f5c` (lead) | read in admin review 4 |
| governance parts 1-3 (`governance_0001`) | `cc6be1a`, `fb9f9f0`, `9047d6e` | review 1 APPROVED (parts 2-3: no migration, not critical). **governance is done** |
| ledger guard fails closed (`ledger_0002`, B-611) | `5f1d9b2` | APPROVED (`docs/build/reviews/ledger-guard-isolation-review.md`) |
| build part 1 (data layer, `build_0001`) | `784c2bd` + `5701758` + `7f05da8` | review 1: BLOCKED, BLOCKED (tests only), APPROVED (`docs/build/reviews/build-isolation-review-1.md`) |

Last clean-checkout gate run: `gates.py --ref build --workers 6 --no-frontend` at `7f05da8` (main's code): 8/8, pytest 1156 passed in 321.9 s. Front end not re-run this session (no front-end change since `4bcc1fb`).

## The exact next step

1. **build part 2** (sonnet, `module-builder`). Reuse branch `build` (worktree `.worktrees/build`, `.devdata/dev.env` written, db `shifde_build`; run `git merge --ff-only main` first). Scope: SETH `build/agents.py` (397), `prompts.py` (667), `skills.py` (228), `tools.py` (439), `tool_runtime.py` (228), `executors.py` (280) — about 2240 lines; if that is too much for one agent under 200k, split into 2a (agents, prompts, skills) and 2b (tools, tool_runtime, executors). Follow the carry_forward row "build (part 2-4)": server-minted ids only, scope and `visibility='private'` enforced in the service, principal columns from the session; its SETH tests by part. Decisions B-1911 on.
2. **build part 3**: `pipelines.py` (928), `pipeline_nodes.py` (429), `orchestrations.py` (376), `kind_ops.py` (608), `catalog.py` (112, B-1903). Split if needed.
3. **build part 4**: `ontologies.py` (969), `ontology_expr.py` (310), `ontology_ops.py` (181), `service.py` (1207), `api.py` (223).
4. **Then, in this order:**
   1. `ontology` (sonnet; candidate for the charter §10 haiku trial if mostly a copy; key the mappings unique constraint and info PK on `tenant_id`, carry_forward row)
   2. `runtime` (opus, 8806 lines: split; C1 confirmed by test before acceptance; remove governance's `to_regclass` guards, B-1812/B-1824; `shifde_ops_stats(int)`; budget alert insert conflict target with the budget's own tenant, B-607; runtime halves of Items 9, 11, 15; client-scoped due-schedule claim)
   3. `app`/`cli`/`main` (mount every router; deferred SETH tests in `carry_forward.md`; close identity review 4 item 5, B-1625, before OIDC is mounted)
   4. the two-org proof (local cluster, S-002)
   5. the remaining front-end areas (B-1107: after `app`)
   6. update STATUS "Known defects"
   7. the factory install (charter §6)
5. Fix the RPM-limit test flake soon (carry_forward row "models_admin (tests)"); it hit two gate runs.

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local and origin) | Up to date with this handoff commit |
| `build` (origin) | At `7f05da8`, merged; reuse for part 2 (`.worktrees/build`) |
| `governance`, `ledger-guard`, `admin`, `models_admin` (origin) | Merged; done. `.worktrees/governance` and `.worktrees/ledger-guard` can be removed (`git worktree remove --force`); `.worktrees/admin` directory left on disk (Windows "Permission denied"), not registered |
| `governance-1` (local only) | Gating pointer; `git branch -D governance-1` |
| `.worktrees/verify`, `verify2`, `verify3` | Detached checkouts for `gates.py --name` |
| Leftovers | `.worktrees/fe-areas-1`, `.worktrees/secrets`, `.worktrees/skeleton`, `.worktrees/_scratch_admin`, `.worktrees/usage.txt`, `S:/tmp_identity_scratch`, `S:/tmp_identity_tools`, `S:/Development_2026/scratch_models_admin`: delete by hand |
| `observe/` (untracked, main checkout) | Not this build's; leave it (listed in `.git/info/exclude`) |
| Local cluster | PostgreSQL 17 on 127.0.0.1:55450, running. Scratch databases `shifde_tmp_review_*` are harmless (`shifde_tmp_review_gov1`, `_guard` remain) |
| Background agents | None running |

## Learned, not recorded elsewhere

- **The handoff rule is "at 300k or more"**, checked after each accepted unit. An earlier attempt this session at 250k was refused by the session's stop condition; keep working until the figure is reached.
- **Overlap pattern that worked:** gate and isolation-review a fixed commit (`git branch -f <unit>-N <sha>`, `gates.py --ref <unit>-N --name verify2`) while the next part builds on the same branch; send review follow-ups to that builder with SendMessage. A resumed reviewer confirming a fix costs little (one confirm took 27 s).
- **Ask reviewers to prove new tests fail on the old code.** Build review round 2 found that 4 of 6 new oracle tests passed on the old keys; the reviewer restored the old PKs as owner in a scratch database and re-ran them.
- **After merging a branch into main with a real merge commit, gate main before pushing.**
- **A "flake" that recurs in the same test is a bug until shown otherwise** (B-1020a: `ObjectDeletedError` after `session.rollback()` on an RLS-scoped object).
- **Known load flakes** (rerun only these, each passes alone): identity cookie-expiry `_same_failure`; `tests/audit/test_service.py::test_concurrent_appends_keep_one_linear_chain`; `tests/connections/test_api.py::test_connection_call_deadline_applies_to_direct_invokes`; `tests/models_admin/test_p3_models.py::test_requests_per_minute_limit_is_enforced` (new; fix it); `tests/models_admin/test_limiter.py::test_db_backed_limiter_counts_atomically_and_leases_expire` failed once in a builder run.
- **Docs merge conflicts:** union, but where the branch narrowed or closed a row take the branch's row; check duplicates with a `Counter` over table lines.
- **Git Bash here:** `/tmp` is not writable (use `.worktrees/`); avoid long multi-heredoc commands (they failed to parse once); `lint-imports` with output redirected to `/dev/null` exits 1 with a `UnicodeEncodeError`.
- **Spend:** about $285 estimated at list price at the first handoff attempt, up from $222.35 at the start of session 5; regenerate with `python scripts/usage_report.py`. It lists `claude-fable-5-2` at $19.50 (80 requests); no agent this session used Fable. The charter forbids it for agents; check `.claude/agents/*.md` and any `model` overrides if it grows.
- Opus builders ran 195-230k tokens per part at 350-550 SETH lines; sonnet builders 158-195k at ~1000-1400 lines.
