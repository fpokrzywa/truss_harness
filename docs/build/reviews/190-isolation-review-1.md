# Isolation review 1, job 190 (#375, PR #391), head 394501c

APPROVED

Isolation review 1, job 190 (#375, PR #391), head 394501c, merge base 831711f. No blocking finding. All four findings below are LOW. None is a cross-client path, and none is a regression. Each needs a Snowflake reading that has not been confirmed, or stays within one client's own least-privileged credential (B-1507).

Findings (non-blocking, follow-up)

1. LOW, `backend/src/shifde/adapters/dataplatforms/sqlapi.py:145-149` (comment branch). Run time only, existing behaviour, recorded as a limit in B-4095.
   - Scenario: `select $1 from snow://a/b ->> remove @s/y` is ACCEPTED by `check_read_only`, because `//b ->> ...` is blanked as a comment. Publish, drafts and render refuse it (`sql_comment`). If Snowflake lexes an unquoted `snow://` (or any `x://`) address as one path, the `->>` is live. This is the same hole as `@s//x`.
   - Reach: a raw `read` invoke only, on the client's own credential.
   - Fix (follow-up): in the Snowflake dialect also refuse `//` directly after `:`. Add it to live test #275.

2. LOW, same lines. A backslash before the character that ends a path.
   - Scenario: `select $1 from @s/a\ //x ->> remove @y` is ACCEPTED at run time, since the space ends the path and `//` is then read as a comment. Publish refuses it (`sql_comment`). It matters only if Snowflake allows an escaped space in an unquoted path; I know of no documentation that says so. B-4095 does not name this case.
   - Fix (follow-up): do not end the path when the previous character is `\`, or list it in #275.

3. LOW, `backend/src/shifde/core/sqlrules.py:65` (`SNOWFLAKE_REFUSED_FUNCTIONS`). The review brief names LAST_QUERY_ID/RESULT_SCAN, but issue #375 and the spec name only three functions.
   - Scenario: `select * from table(result_scan(last_query_id()))` is accepted at run time and refused at publish (`sql_dynamic_name`). `select * from result_scan('<id>')` and `select last_query_id()` are accepted by the publish statement rules and at run time; I did not confirm that Snowflake accepts RESULT_SCAN without TABLE. From Snowflake's documented behaviour, not tested: RESULT_SCAN returns only results of the same Snowflake user, and every SQL API statement has its own session. The result stays within the connection's credential and grants, so it is never another client's data unless two clients share a credential.
   - Other functions still open: `to_file('@s/x')`, `getnextval(...)` and, at run time, `table(information_schema.query_history())`. B-4096 covers these in general ("other functions ... still not read") but does not name RESULT_SCAN.
   - Fix (follow-up): add `result_scan`, `last_query_id` and `getnextval` to the shared set, or name RESULT_SCAN in the accepted limits.

4. LOW, `backend/src/shifde/core/sqlrules.py:361-382` and the run-time word rule. An exact-name match does not see a name that ends in an invisible letter.
   - Scenario: `get_ddl` + U+3164 + `('a')` passes both checks, because U+3164 is a `\w` character. U+200B is refused. It works only if Snowflake drops U+3164. Drafts and render refuse non-ASCII outside quotes. The spec puts look-alike letters out of scope. `"IDENTIFIER"('get_ddl')('a','b')` is the other accepted limit in B-4096; it passes as listed.

What I checked

- Scope: 12 files, no migration, no schema or route change, no OpenAPI or protected path. No new query, table, job, cache or grant, so the RLS, route and role-binding checks do not apply. The refusal messages are fixed constants and never echo the SQL.

- Stage path rule: in the run-time reader, the `path` flag is tested before the quote, `$$` and comment branches, and each code character (each `@` included) passes the branch that sets it. In publish's `_stage_path_mark`, `@` and the path-end characters are never inside a token, except in a comment or quote, which is already refused when `path` is set. Every mark starts a token: `_snowflake_name_end` stops at `$$`, and numbers do too. The rule takes the fewest path ends, so a shorter Snowflake path can only cause over-refusal. Both are linear.

- Bypasses tried and refused at run time, and at publish wherever the text is publishable:
  - marks `--`, `//`, `/*`, `'`, `"` and `$$`, terminated or not, directly after `@`, after U+00A0/VT/FF, and after `)`, `,` and `;`;
  - nested comments;
  - comment gaps: `@s/x/**/ ->>`, `seq./**/nextval`, `:identifier /**/ (`;
  - `:` + space, newline or comment + word;
  - `::nextval`, `a:nextval.b`, `a:b.nextval`, `:"GET_DDL"(...)`, `:"system$x"`, `"nextval"`;
  - `get_ddl` + U+200B + `(`;
  - `identifier('get'||'_ddl')`, `identifier("GET_DDL")`, `identifier($$get_ddl$$)`, escaped literals and bound values in any case;
  - `%%(call)s`, `@%%(call)s//x`, `@%(a)s/x//y ->>`, `@s/:call`;
  - `"IT'S", %(call)s, 'x'`, where `positional_sql` misreads the quote and leaves a raw `:call`; it is refused by `raw_word_rules` and at run time.

- Raw words: `raw_word_rules` runs on exactly the text `tool_runtime.sql_request` sends (`positional_sql(template)[0]`), for Snowflake only, with the same maximal-word split as run time. `?` gives no issue, and `%(call)s` still binds. Render refuses any undeclared raw `:name`, so a raw word does not become statement text or an identifier.

- Nothing loosened: the refactors (`_system_name` to `_code_name`, `_dynamic_name` split, `_refused_param`, IDENTIFIER literal) are supersets of the old rules. My own differential fuzz against origin/main's source, 2 seeds x 150,000 templates over the bypass alphabet, with bound params:
  - 0 statements refused at run time by main are now accepted, on both the raw template and the positional text;
  - 0 publish statement issues of main are missing;
  - 0 differences on Databricks (both checks);
  - 0 unexpected exceptions;
  - new run-time refusals: 16,416 in seed 12 (seed 11's count not read);
  - 0 templates that pass publish but are refused at run time, once the existing `invalid_template` forms (`%(a)sword`) are excluded. Publish is therefore no looser than run time on that alphabet; the reverse (publish stricter) is by design.

- Tests: 1039 passed over `test_sqlrules_snowflake_reads`, `test_snowflake`, `test_render_snowflake`, `test_sqlcheck`, `test_snowflake_tool_validation`, `test_sqlrules_snowflake` and `test_tool_runtime_snowflake`. Only one import line is removed from existing tests; no assertion or list entry is removed. New refusals are asserted to send no request to the fake account.

- Docs: B-4095 to B-4097 and the design texts match the code, claim no live confirmation, and keep `.e5from` open (S-043, S-049). The accepted points (`:identifier(%(a)s)` refused at publish; `.e5from` open) were not counted.

- No files changed in the worktree (git status clean); temporary files removed. No secret file opened.
