# Isolation review 2, job 172 (#272 SP6-5, PR #353) at c98948c

APPROVED (isolation-reviewer, 2026-11-24; base origin/main 48bd54a; saved by the lead from the reviewer's report, shortened). No path from one SETH organization to another. Findings 1-3 of review 1 are fixed. Findings 4-6 are not blocking and are filed as a follow-up issue.

1. FIXED: `appgen/render_snowflake.py:72-84` refuses any data-read name that is not three plain ASCII parts, as written, in every `data_reads` entry (409 `release_not_renderable`); `templates/snowflake/deep_health.py.txt:45-50,69-71` matches and double-quotes each part, any other name fails its check without SQL. 20 `BAD_TABLES` and 14 more hostile names run: none rendered, `_quoted` accepted none.
2. FIXED: `render_snowflake.py:87-97` refuses an undeclared `:name`; values always bound.
3. FIXED: `statement_rules(..., dialect="snowflake")` runs on every tool at render.
4. Not blocking (fix before a live Snowflake app): the generated app runs `tool["sql"]` with less checking than `sqlapi.check_read_only`. Reproduced: `select system$wait(5)`, `select "SYSTEM$WAIT"(5)`, `select * from identifier(:region||'x')` render; a bound `SYSTEM$...` value is not refused. Contained to the client's own account by the application's grants.
5. Not blocking, pre-existing on main (`core/sqlrules.py:53-55`, `adapters/dataplatforms/sqlapi.py:159-195`): the Snowflake flow operator `->>` chains a second statement; only the first word of the statement is checked. Reproduced: `select 1 ->> execute immediate 'drop table x'` and similar pass both checks. Own client only; also weakens SETH's read tools.
6. Not blocking (same as Databricks since SP5): `render_snowflake.py` does not compare tool SQL with `manifest.data_reads`.

No finding on routes, migrations, roles, app-preview scoping, dispatch, the generated app's session and bound values, the Databricks split, or tests (nothing weakened). 156 targeted tests passed; full suite not run.
