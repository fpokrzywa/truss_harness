# Job 074 (#128, PR #193) lead isolation review: APPROVED

Reviewer: isolation-reviewer, 2026-11-22, branch factory/074-sp4-5-cli-backup vs origin/main. Recorded by the lead.

1. No cross-client path: no tenant setting or new request/worker route; restore refuses any target user but the owner, uses `--no-owner`; `restore_checks` reruns the app-role startup check; `test_restore.py::test_the_restored_copy_keeps_the_organizations_apart` shows demo-a cannot list demo-b's secrets or runs and gets the same 404 for demo-b's run as for an unknown id.
2. No credential or plaintext leak: password only in PGPASSWORD; no `SHIFDE_*` reaches pg_restore (tested); DB errors print the class name only; `check_all` discards plaintext, prints ids only (tested).
3. Advisory: `_check_fingerprint` compares against the unsigned manifest only; a forged backup with a recomputed manifest could weaken policies and still print RESTORE VERIFIED (accepted in the module docstring). Follow-up issue filed.
4. Advisory: `check_all` / `_check_audit` rely on the owner having BYPASSRLS; without it standalone `secrets check` prints "ok 0, failed 0". Follow-up issue filed.
