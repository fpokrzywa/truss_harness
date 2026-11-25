# Isolation review 1: job 181 (#358, PR #369, head 3ccfd5e)

Saved by the lead from the read-only isolation-reviewer, unchanged.

BLOCKED

Scope: 181 at 3ccfd5e against origin/main. No cross-client leak: the diff touches no table, policy, grant, route, job or session setting. Every finding stays inside one client's own Snowflake account. The ->>, SYSTEM$ and procedure rules hold at all three points. The block is on the new data_reads check at render (issue item 3): it can be passed with undeclared reads. "RENDERED" = I ran appgen.render on SNOWFLAKE_MANIFEST (data_reads CUSTOMERS, ORDERS) and the SQL reached ui/config/tools.json. "passes" = statement_rules and table_rules give [] and check_read_only accepts. Nothing was run on a live Snowflake account.

Paths are under S:\Development_2026\shi_work\agt_trus_harness\.worktrees\181-snowflake-sql-checks-refuse\

BLOCKING

1. backend\src\shifde\core\sqlrules.py:341-355 (_dynamic_name) with backend\src\shifde\appgen\render_snowflake.py:96. IDENTIFIER('lit') and TABLE('lit') are accepted, and table_rules never reads the literal. It refuses the call only directly after from/join, or in a comma list starting right there. So a fully qualified undeclared table gets through:
   - RENDERED: select * from (identifier('DEMO_DB.PUBLIC.SECRETS')) s where s.r = :region
   - RENDERED: select s.* from DEMO_DB.PUBLIC.CUSTOMERS c join DEMO_DB.PUBLIC.ORDERS o on o.customer_id=c.id, identifier('DEMO_DB.PUBLIC.SECRETS') s where c.region = :region
   - passes: same join form with table('...'); CUSTOMERS sample (10), identifier('...'); CUSTOMERS at(offset => -60), identifier('...'); (table('...') s); with "LATERAL" as (select 1) ... , lateral table('...') s
   - passes: select identifier('DEMO_DB.PUBLIC.UDF')(1) from ... The plain spelling DEMO_DB.PUBLIC.UDF(1) is refused, so the literal also hides a UDF (possibly one with external access).
   Scenario: a tool author names SECRETS this way. Release and plan list only CUSTOMERS and ORDERS, the approver sees those, and the app reads SECRETS under owner's rights wherever it has the privilege. This contradicts the new docstring ("reads only `tables`", render_snowflake.py:87-91) and B-4049.
   Fix: in table_rules, Snowflake dialect, refuse any unquoted IDENTIFIER or TABLE word followed by "(" anywhere (sql_table_function). statement_rules and run time can keep accepting the literal, so spec cases and tests of statement_rules stay. Add render tests for the forms above.

2. backend\src\shifde\core\sqlrules.py:445-473 (table_rules.reference; old code, newly relied on at render). One- and two-part names are checked only directly after from/join or in that from's comma list:
   - RENDERED: select s.* from DEMO_DB.PUBLIC.CUSTOMERS c, (APP_DATA.USAGE) s
   - passes: ... c join ORDERS o on ..., APP_DATA.USAGE u (a join's comma list is not followed)
   - passes: from (secrets) and from (CUSTOMERS, secrets) (a parenthesised name is taken for a subquery)
   - passes: CUSTOMERS c (a,b), secrets s (nor after sample(...), at(...), x(a))
   - passes: with a as (select * from b), b as (select 1 x) select * from a (a forward reference reads the real b)
   - passes: select *, 1from APP_DATA.USAGE u (number token swallows `from`; whether Snowflake lexes it so is unconfirmed)
   APP_DATA.USAGE is created by the app itself (templates\snowflake\setup.sql.txt:4), so the owner's rights always cover it. The spec's out-of-scope line names over-refusals (lateral, extract, is distinct from), not these misses.
   Fix (fail closed, Snowflake): at "(" after from/join/comma, unless the next token is select/with/"(", check the content as a table list. Continue comma lists until the clause ends at that depth. A CTE name counts only after its own definition. Do not swallow name characters after a number. At minimum every `from (name` and every comma entry after a join must be checked.

3. backend\src\shifde\core\sqlrules.py:450-452 (`if not parts: return i`). Stage reads give no issue:
   - RENDERED: select $1 from '@DEMO_DB.PUBLIC.STG/secrets.csv'
   - passes: from $$@DEMO_DB.PUBLIC.STG/secrets.csv$$ ; from @~
   A staged file (internal or external stage) is never in data_reads. The unquoted @DEMO_DB... form is refused by name only.
   Fix (Snowflake): after from/join/comma, anything but a name or "(" is an issue; a string, or any "@" between the keyword and the name, is refused.

NOTES (not blocking)

4. backend\src\shifde\adapters\dataplatforms\sqlapi.py:124 (old) with :180 (new). Run time blanks // and -- to end of line before looking for ->>. `select $1 from @s//x ->> execute immediate 'drop table x'` and `select $1 from @s/a--b ->> remove @s` pass check_read_only. If Snowflake reads // or -- as part of an unquoted stage path, the operator is live. Publish, draft and render refuse both (sql_comment); only already-published tools and direct read invokes are affected. Suggest refusing //, -- or /* directly after a stage-path character, or confirm live (#275).

5. sqlrules.py:347: `identifier\u200b(:t||'x')` passes both checks (str.strip and \s do not match it). Safe only if Snowflake errors on it. Cheap fix: flag when the gap holds anything but whitespace and "(" follows.

6. Functions reading objects by string (get_ddl, get_presigned_url) and seq.nextval still pass everywhere. Old, documented as best effort (B-1507).

7. Publish against run time: 600,000 random statements built from the new tokens. Accepted at publish but refused at run time (safe direction): a raw `:call`/`:drop` (publish reads a parameter) and `;` followed by characters that make no token. Rendered text had only the `;` case; the generated app has no check. `select 1; <non-token characters>` is harmless as far as I found, but refusing any `;` but a trailing one in sqlrules.py:288 (`tokens[:-1]`) would close it. No accepted text held ->>, SYSTEM$ or procedure as code after positional().

8. Previously refused, now allowed: none found. Only refusals were added; the Databricks branch of tokenize, WRITE_KEYWORDS and Postgres are unchanged.

9. Existing tests, each edited line read:
   - tests\appgen\test_routes_cross_client.py:283: the one removed line swaps SF_TABLES for the helper's SAMPLE_TABLES. The refusal (_same), the owner's 200, generator version and file list are still asserted. Not weaker.
   - tests\appgen\test_render_snowflake.py:120 (one tool kept) and :277 (one tool reading the parametrized table): assertions unchanged.
   - Parametrized lists only grew; nothing removed or skipped.

10. New tests against origin/main's src (plus a FLOW_OPERATOR_MESSAGE constant so the module imports): 10 fail in test_render_snowflake, 88 in test_sqlrules_snowflake, 4 of 4 selected in test_snowflake (-k flow). Passing without the change: accepting and parameter-name cases, test_a_declared_table_in_another_letter_case_renders, the ACCEPTED_SQL additions (positive controls), and refusing cases old rules already caught. The never-raises test was not run against old code. On the branch, 179 selected tests pass. Not run: database-backed tests (pytest with the normal conftest exceeded my time limit; I used --noconftest) and the full suite.

11. DECISIONS B-4049 (4) and the SP6 design 6.3 line ("a tool that reads another table is 409") overstate the check until 1-3 are fixed.

Worktree unmodified (git status clean); scratch files removed.
