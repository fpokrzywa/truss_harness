# Isolation review: job 112 (PR #217)

Branch factory/112-secrets-check-fail-when, commits 835b702 and 2405d7b vs origin/main. Reviewer: isolation-reviewer (lead session 6975aeb6, 2026-11-22).

Verdict: APPROVED. 0 findings.

1. `core/dbrole.py`: only a new read-only `sees_all_rows(session)` (`rolsuper OR rolbypassrls` for `current_user`). `check_app_role` and the startup refusal list unchanged; only caller is `check_all`.
2. `secrets/service.py`: `check_all` refuses before reading any row unless the role sees every row. Stricter, not weaker; fixed message naming no id, URL, role or key.
3. CLI callers (`cli/main.py`, `cli/backup.py`) turn the refusal into exit 1 / `FAILED secrets:`; no leak (tested in `tests/secrets/test_check_all.py`).
4. No migration, policy, grant, route, job or role-binding change.
5. Startup role check test, RLS schema test and cross-client tests untouched; three tests added, none removed or weakened.

Out of scope (B-3091, follow-up #218): `rewrap_all`, `_check_audit`, restore tenant listing have no such guard; under the app role they only see fewer rows.
