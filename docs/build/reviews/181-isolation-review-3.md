# 181 isolation review 3 (#358, head 101c0e4)

APPROVED

Isolation review 3: job 181 (#358, PR #369), head 101c0e4, against origin/main f49addf (merge base 8212720).

Scope: the diff touches no table, policy, grant, route, job, session setting, template or migration. It is Snowflake SQL rules, the Snowflake render gate, tests and docs. `git merge-tree` with origin/main gives a clean tree; main has not touched the changed source files since the merge base.

BLOCKING: none.

1. Is review 2 finding 1 (`1.e5from`) closed? Yes, for every spelling where a digit starts the number.
   - The fix is `_glued_number`, backend/src/shifde/core/sqlrules.py:498-510 with :75-76. It matches each digit run through the following dots and name characters, past the token span, and requires the whole run to be a number.
   - I ran 40,800 statements: 85 number spellings x 8 keywords x 12 sources x 5 prefixes.
     - Numbers: hex and hex with dot, binary, octal, underscores, trailing, leading and double dots, exponent signs, `1e+`, `1e0e0`, suffix letters, `$`, `$1.`, numbers after a string, quoted name, paren or bracket, Arabic-Indic, fullwidth and superscript digits, zero-width, BOM, NUL, soft hyphen, combining mark.
     - Before: from, join, lateral, from lateral, from table, from identifier, union select.
     - Sources: one- and two-part names, quoted names glued on, parenthesized, stages, table()/identifier(), another database.
   - No from/join/union glued to a digit-started number passes. What still passes is in findings 2 and 3 (not live).
   - Independent oracle: a separate maximal-munch lexer (number with optional dot and signed exponent, then word) on 480,000 random statements, 62,696 accepted. None has a FROM or JOIN the rules did not see.
   - `1.e5`, `1.`, `1.5e3`, `2.` and `t1.e5x` still pass.

NOT BLOCKING

2. sqlrules.py:283-285. A dot with no digit directly before it, then a glued word, still passes: `select .e5from secrets`, `select 1 .e5from ...`, `select 1. e5from ...`, `select 'a'.e5from ...`, `select (1).e5from ...`, `select :p1.e5from ...`, `select $x.e5from ...`. Main gives the same. I do not count these as live: they hide a FROM only if the platform lexes `.e5` (no digit) as a number, and that would break every `t.e5` column reference. A published ANTLR Snowflake grammar, from memory, requires a digit. Nothing was run on a live account. A cheap over-refusal if wanted: refuse an ident matching `e[0-9]` plus a name character directly after a `.` that does not directly follow an ident, `]` or `)`. It could go to #375 with the live test #275.
3. A bare `lateral` after a dot outside a FROM clause (`select 1 .lateral x`) passes. It names no source there; inside a FROM clause it is refused.
4. sqlrules.py:513-538 (`_ends_from`). Review 2 finding 2 is fixed. I tried 13 clause starters x 50 positions x 4 tails (after `.`, `::`, `$`, `:`, `[`, `{`, operators, `as`, casts, numbers, strings). What still ends the list is either a real clause start or text I believe Snowflake does not parse (`collate where`, `is where`, `case where`, `c.union select`, `c.union all select`, `[union select`, `... name select 1, secrets`). I see no valid statement that hides a comma source. To tighten: for SELECT, apply the same `.`/`::`/operator test to the set-operator word before it.
5. `... union all table secrets` and `x in secrets` pass, unchanged from main. I know of no Snowflake form where either reads a table.
6. #375 items (stage-path comment marks, get_ddl/nextval, raw `:word`) are not worse. Review 2 notes 3 to 6 stay as recorded.

DOES ANY CHECK ACCEPT WHAT origin/main REFUSES? No. Side by side, 1,200,000 random statements in 16 runs:
   - statement_rules: 0
   - table_rules: 0 (counted where the new statement rules pass)
   - check_read_only, Snowflake: 0
   - Databricks tokenize, statement_rules, table_rules, check_read_only: 0 differences
   - parameter_rules: 0 differences in the last 10 runs. The first 6 runs compared objects of two module copies, my script's error, so their count means nothing.
   - Head against 2e47825: nothing refused there is accepted now.

   Source read: sqlapi.py only adds refusals (flow operator at :181, characters before `(` at :236), and the shared constants equal the old literals. schemas.py:67 keeps the 8000 limit.

RENDER (render_snowflake.py:95-114): length, then statement rules, undeclared parameters, then table rules over the union of data_reads; `_tables` still runs first. `positional` replaces whole param tokens with `?`, so it creates no new glue. Also tried and refused: directed, asof and natural joins, from-first, match_recognize/unpivot/changes/tablesample then a comma source, values, nested and forward CTEs, stage joins, string-, `]`-, `}`- or `)`-glued from, and \x0b, \x0c, \x1c, \x85 before from. Accepted tables are only the declared three-part names; a one- or two-part name in a FROM clause (APP_DATA.USAGE) is refused.

COST: 8000-repeat inputs (8 to 96 KB) take at most 0.55 s; render cuts at 8000 characters first.

TESTS
   - No test removed, renamed, skipped, xfailed or loosened. Removed test lines in the whole diff: two docstring lines, the SF_TABLES input line in tests/appgen/test_routes_cross_client.py (assertions intact), the render call in test_a_plain_table_name_renders_uppercased (input only, assertion intact), and one parametrize list that only grew. 2e47825..HEAD removes no test line.
   - The finding 1 and 2 forms are in TABLE_RULES_REFUSE (tests/build/test_sqlrules_snowflake.py) and in the render refusals (tests/appgen/test_render_snowflake.py).
   - The diff adds no route, so no new cross-client test is needed.
   - Run from backend (`uv run --frozen pytest -q -n 8 -p no:cacheprovider`) on tests/build/test_sqlrules_snowflake.py, test_sqlrules.py, test_snowflake_tool_validation.py, test_tool_runtime_snowflake.py, tests/adapters/dataplatforms, tests/adapters/test_postgres_read_only.py and tests/appgen: 1556 passed, 3 skipped (the three opt-in live tests), exit 0, 1007 s.
   - Not run by me: tests/build (rest), tests/delivery, tests/packaging, tests/connections, the full suite. The caller still needs green gates on the synced head.

DOCS: B-4067 (S:\Development_2026\shi_work\agt_trus_harness\.worktrees\181-snowflake-sql-checks-refuse\docs\build\DECISIONS.md) and SP6 design 6.1/6.3 match the code.

The worktree is unmodified (status clean, head 101c0e4). My scratch scripts in the system temp directory are removed.
