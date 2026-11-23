# 143 (#285, PR #308) lead isolation review 1 — 2026-11-23

APPROVED, no cross-client finding.

1. Only adds refusals: Snowflake `SYSTEM$` words/identifiers (`sqlapi.py:171-174`); Postgres `U&`/`UESCAPE`, backslash in `"..."` identifiers, side-effecting functions and `dblink_` prefix (`postgres.py:110-111, 120, 154-161`). Databricks and `WRITE_KEYWORDS` unchanged.
2. `_code_of` now returns four values; its only caller `check_read_only` is updated.
3. No route, database, RLS, grant, credential or audit change; refusal messages are constants and never echo SQL.
4. New test `tests/connections/test_read_only_invoke.py:113` runs in client A; existing test covers client B's 404. No new route, so no new cross-client test needed.

Follow-up (non-blocking, not cross-client): dynamic SQL in string literals (`query_to_xml`, `query_to_xml_and_xmlschema`, `cursor_to_xml`) and read functions not listed (`pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_get`); limiting control remains the least-privileged credential (B-1507). Filed as #312.
