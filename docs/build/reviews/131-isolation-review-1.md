# Isolation review 1: job 131 (SP5-1, issue #254, PR #264)

Reviewer: `isolation-reviewer`, 2026-11-23, diff `factory/131-sp5-1-sql-tools` vs `origin/main` (round 2 head `8d7974e`). Verdict: **APPROVED**. (The reviewer is read-only; the lead recorded its report here.)

1. `connections/service.py:1376-1387`: `open_for_use` still loads through `_get_row` (organization-filtered, same 404 as unknown id); `at` and `connection:use`/`connection:write` checks unchanged; `UseSession.connection_type` comes from the re-read row. No change needed.
2. `build/tool_runtime.py:207`: `sql_request` picks the request format from the re-read row's type; a spec cannot force a format. Parameter types limited to five values. No change needed.
3. `build/tools.py:136-146`: `_maturity` looks up through `env.connection` (own organization, readable only); another client's id is `connection_not_found`. Covered by `tests/build/test_databricks_tool_validation.py:299`.
4. `connections/schemas.py:243`, `adapters/dataplatforms/databricks.py:212-234`: `param_types` is a closed set; `max_rows` only lowers the cap. Neither changes connection, credential or organization.
5. `tests/build/test_tool_runtime_databricks.py:258`: cross-client test (client B names A's connection through `execute` and `invoke_once`, as app role and as owner role) gets 404 with no workspace request; control succeeds. Sufficient.
6. (low, informational) `adapters/dataplatforms/fakes.py`: the fake workspace's statement store and sample SQLite are shared across callers; acceptable because the fake is development/test only and sample data is not enabled in the development router (`fakes.py:711`). Possible follow-up: check the bearer token on statement-result fetch.
7. No migrations, RLS policies, grants, SECURITY DEFINER functions, routes, jobs or caches added; the SQL invoke body change has a 422 test (`tests/connections/test_sql_invoke_param_types.py:36`).
