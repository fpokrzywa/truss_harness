APPROVED

Lead isolation review of PR #236 (job 121, issue #218), origin/main...origin/factory/121-refuse-rewrap-audit-check (f85d321, 4dccf8f), by isolation-reviewer, session 2026-11-23.

1. backend/src/shifde/secrets/service.py:279: `rewrap_all` refuses (`RowSecurityHidesSecrets`) via `sees_all_rows` before reading rows under row-level security; a fixed message, no ids or keys; `check_all` unchanged. Only narrows.
2. backend/src/shifde/cli/main.py:301: `secrets rewrap` exits 1 before commit; only caller is the CLI on the owner session; no request or worker path gains an owner engine.
3. backend/src/shifde/cli/backup.py:679: `_check_audit` returns `ROW_SECURITY_HIDES_AUDIT` before the cross-tenant listing, so a vacuous pass under the app role now fails and `RESTORE VERIFIED` is withheld. No tenant slugs in detail.
4. backend/src/shifde/core/dbrole.py:200 `sees_all_rows` unchanged (checks current_user rolsuper/rolbypassrls).
5. No migrations, policies, grants, routes, SECURITY DEFINER functions; startup role check untouched; no cross-client test removed or weakened.
6. Tests: two-client rewrap refusal under app role (kek ids unchanged, no secret material in output) and restore audit-check refusal (exit 1).

No blocking findings.
