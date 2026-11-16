# runtime-merge — unit notes (lead session, 2026-11-16 16:24-17:00)

## State
- Branch `runtime` = `7bcab1c` (RT-1..RT-12 + B-2150 edit), pushed, unchanged by this session except this notes file.
- Merge NOT started; merge lock NOT taken. Unit brief (S-008): take the merge lock only once `units.json` on origin/main shows `nextjs-security` done. At 16:56 it was `todo` (not started), so this session handed off.

## Trial rebase (done, pushed as branch `runtime-rebase-trial`, head `8da652bb`)
- `runtime` rebased onto origin/main `8f67a20`; all 16 commits applied.
- Conflicts resolved: `tests/schema/test_function_schema.py` (allowlist keeps main's `shifde_installation_idp_secret(uuid)` and runtime's `shifde_claim_jobs`, `shifde_reap_expired_leases`, `shifde_ops_stats`; docstring names secrets_0003 and runtime_0001); `DECISIONS.md`, `carry_forward.md` union; `STATUS.md` kept main's rows on every RT commit (main's `runtime` row already covers RT-1..RT-12); `backend/pyproject.toml` scripts = `shifde-api`, `shifde-worker = shifde.runtime.worker:run`, `shifde-admin = shifde.cli.main:app`.
- Alembic heads on the rebased tree: main's 13 plus `runtime_0001` (no alembic.ini; list heads through `shifde.core.migrations`).
- Checks on `8da652bb`: ruff format, ruff check, mypy src (179 files), lint-imports (5 kept): all exit 0. `pytest -n 8 tests/schema tests/runtime tests/ledger tests/governance tests/ontology`: 1033 passed in 1105 s.

## Exact next step
1. Wait until origin/main `units.json` shows `nextjs-security` done.
2. `python scripts/unit.py lock merge --unit runtime-merge` (scripts live on main; on `runtime` they appear after the rebase).
3. `git fetch`; `git checkout runtime`; `git reset --hard runtime-rebase-trial` is NOT allowed to reach origin/runtime by force — instead: `git checkout -B runtime-merge-local origin/runtime-rebase-trial` and `git rebase origin/main` (only new main commits can conflict; the old resolutions are already in it). Do not force-push `runtime`.
4. Update STATUS.md runtime row, DECISIONS.md, units.json `runtime-merge` -> done; commit.
5. `python scripts/gates.py --ref <full sha> --name runtime-merge-merge` (13 gates; the cluster must be up).
6. `git push origin HEAD:main`; `python scripts/unit.py unlock merge --unit runtime-merge`; `python scripts/unit.py done runtime-merge`.
