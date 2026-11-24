# Isolation review 1, job 172 (#272 SP6-5, PR #353) at 66c73ba

BLOCKED (isolation-reviewer, 2026-11-24). No path from one SETH organization to another; one injection into generated SQL run at the client.

1. BLOCKING. `appgen/templates/snowflake/deep_health.py.txt:51` runs `f"SELECT * FROM {table} LIMIT 0"` with `table` unquoted and unvalidated. `render_snowflake.py:69-75` only uppercases `manifest.data_reads[].tables`; `packaging/schemas.py:25` `TableName` is free text and `table_name_supported` runs only for app requests (`appgen/service.py:437`), not for a directly created solution version. `["DEMO_DB.PUBLIC.ORDERS UNION ALL SELECT * FROM DEMO_DB.SALES.INVOICES --"]` and `["(select system$wait(5)) --"]` seal, render and reach the session verbatim (reproduced). Fix: (a) deep health runs only a name of exactly three parts, each a full match of `[A-Za-z_][A-Za-z0-9_$]*`, emitted uppercased and double-quoted; otherwise that check fails without SQL; (b) `render_snowflake` refuses such a manifest before any bytes (three parts plus `sqlrules.table_name_supported(t, "snowflake")`) with an `AppError` 409/422; (c) tests: those payloads plus `;`, `"`, `TABLE(f())`, a newline, 2 and 4 parts, refused at render, and `deep_health.run` on a hand-written `agent.json` sends no such statement.
2. Not blocking. `render_snowflake.py:46-58` `positional()` treats every `:name` as a parameter while SETH binds `%(name)s`; refuse at render when a `param_order` name is not a declared parameter.
3. Not blocking. Refuse at render any tool with `statement_rules(sql, dialect="snowflake")` issues (a tool published before the rules).
4. Note. `test_generated_snowflake_app.py:437-465` proves digest separation, not Snowflake isolation (matches requirement 22).

No finding on: migrations, grants, roles, `app-preview` scope, cross-client route tests, SETH callbacks or secrets in the app, bound parameters, the Databricks split, loosened tests or contracts.
