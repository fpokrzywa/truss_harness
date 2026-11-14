# Handoff — 2026-11-13, end of session 2 (lead), updated after the ledger merge

Read `docs/build/CHARTER.md`, then `docs/build/STATUS.md`, then this file. Session 2 handed off at 281k lead tokens (charter §10), between units, with no agent running.

## Finished in session 2

- **skeleton**: five fix rounds against isolation reviews 1–5; review 6 APPROVED; merged (`c925793`). Reviews in `docs/build/reviews/skeleton-isolation-review-1..6.md`.
- **core**: merged (`a814a53`); review APPROVED.
- **adapters**: merged (`0446a0c`); review APPROVED.
- **observability**: haiku trial, passed in one round; merged (`82b3b66`). B-011: haiku for small, non-critical straight copies.
- **authz**: merged (`74e6a83`); review APPROVED with follow-ups.
- **ledger**: review 1 BLOCKED, fix round 1, review 2 APPROVED; merged (`36bd4db`). Main: 529 tests, 8/8 backend gates.
- **New tooling**:
  - `scripts/gates.py` is the clean-checkout gate runner (B-012).
  - `docs/build/carry_forward.md` lists what each port owes later modules: deferred SETH tests, wiring and isolation items. **Every module brief must include that module's rows from this file.**
- Main and origin/main are at the commit that carries this handoff. The backend suite on main passes 529 tests in ~64 s with `-n 8`. The front end was last run in full at `c925793` (13/13) and is unchanged since.

## The exact next step

`ledger` is merged (`36bd4db`, review 2 APPROVED). Next: **audit** (layer 3, the last module of the layer; plan Task 7; sonnet). It is not a straight copy:
- `audit_events.write_own` is on B-112's rejected list (WITH CHECK `... OR tenant_id IS NULL`).
- ARCH m7 is the same defect.

Then the layer order from the plan: `secrets` (opus), `identity` (opus; split by file group per B-013), `tenancy` (opus; split), `installation` (minimal; haiku if a straight copy), `vendors`, `connections` (opus; C2 test), `admin` + `models_admin` (opus; SHI provider switch), `governance`, `build` (split), `ontology`, `runtime` (opus; split; C1 test), `app`/`cli`/`main`, the two-org proof, then the factory install (charter §6).

## Open branches, worktrees and databases

| Item | State |
|---|---|
| `main` (local, origin) | Up to date with this handoff commit. |
| `ledger` (local, origin) | `3e9f6d5`, merged to main. Worktree `.worktrees/ledger` (clean) can be removed. |
| `skeleton`, `core`, `adapters`, `observability`, `authz` (origin) | All merged to main. Their branches can stay. |
| `.worktrees/verify`, `.worktrees/verify2` | Detached checkouts used by `gates.py`. Reuse them; `gates.py` resets and cleans them. |
| `.worktrees/skeleton` (directory only) | Git no longer tracks it; Windows refused to delete it (permission denied, probably a locked `.venv` file). Harmless. Delete it later with `robocopy <empty> <dir> /MIR`. |
| Local cluster | PostgreSQL 17 on 127.0.0.1:55450, running. Leftover databases `shifde_<worktree>` (skeleton, core, adapters, observability, authz) are harmless. |
| Background agents | None running. |

## Learned, not recorded elsewhere

- **Gate runs:**
  - `python scripts/gates.py --ref <ref> [--no-frontend] [--name verify2]` takes about 1–2 min backend-only and about 2.5 min with the front end.
  - Run it from the main checkout. It needs the cluster running.
  - Two gate runs in parallel need different `--name`s.
  - pytest wall time roughly doubles while builders run tests on the same cluster.
- **Subagent reports** come back via SendMessage hand-back. Resuming a finished agent with SendMessage keeps its context, which is cheap for a small follow-up. Do it only while the agent is well under 200k; otherwise start a fresh agent with a short brief (B-013).
- **Gate bypasses:** builders repeatedly found and fixed the same core bug, so tell them about it up front: the checkout hook's `DISCARD ALL` vs psycopg auto-prepare. It is fixed on main by `prepare_threshold=None`.
- **Isolation reviewer:**
  - Reviews of the skeleton gates went 6 rounds before the calibration (B-014) was stated.
  - Give every reviewer that calibration and the list of protected tests (RLS schema, function schema, role check, cross-client).
  - Ask it explicitly to check any change to those tests.
- **Builders over budget:** module builders go over the 200k sizing on modules with several tables (`ledger` 253k, `authz` 290k). Split them (B-013).
- **Fable usage:** the usage report shows about $19.50 of Fable (`claude-fable-5-2`) spend, from session 1 or the supervisor, not from this session's agents. Session 2 used only opus, sonnet and haiku. Charter §4 forbids Fable for agents; keep it that way.
- **Merges to main:** `DECISIONS.md` and `carry_forward.md` conflict on every parallel merge. Keep both sides. Each builder gets its own decision-id range: B-4xx authz, B-5xx observability, B-6xx ledger. Use B-7xx for audit, B-8xx for secrets, and so on, or B-<module-number>xx.
- **SETH tests:**
  - Most SETH test files need the full app `World` fixture. Lower modules therefore move few SETH tests and write direct tests instead.
  - The deferred SETH tests must move with the module named in `carry_forward.md`; the `app`/`cli` ports will pick up most of them.
  - Track test counts: 529 on main now.
- **Hosted database:** still waiting on the owner (PostgreSQL 16.13, no TLS; B-009). Unchanged.
