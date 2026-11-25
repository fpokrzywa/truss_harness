# 181 isolation review 2 (#358, head 2e47825)

BLOCKED

Isolation review 2: job 181 (#358), head 2e47825, against origin/main 5cd5e1a (merge base 8212720).

There is no cross-client leak: the diff touches no table, policy, grant, route, job, session setting or template. Every form from review 1 and security round 1 is now refused, except the #375 forms. One new spelling of review 1 finding 2 (`1from`) still hides FROM/JOIN from the new table rules, and it reached ui/config/tools.json. The fix is a few lines. Main has the same gap, so this is not a regression. Nothing was run on a live Snowflake account.

Paths are under S:\Development_2026\shi_work\agt_trus_harness\.worktrees\181-snowflake-sql-checks-refuse\

BLOCKING

1. backend\src\shifde\core\sqlrules.py:279-283 (tokenizer) with :534 (glued-number check). The number check only looks at text between tokens. In `1.e5from` the tokenizer makes number `1`, punct `.`, ident `e5from`, so no `from` token exists and no number run is left in a gap. Publish, table rules, run time and render all pass. RENDERED (release lists only CUSTOMERS and ORDERS):
   - select u.*, 1.e5from APP_DATA.USAGE u where u.x = :region
   - select s.* from DEMO_DB.PUBLIC.CUSTOMERS c join DEMO_DB.PUBLIC.ORDERS o on 1 = 1.e0join APP_DATA.USAGE s on s.x = :region
   - select $1, 1.e0from '@DEMO_DB.PUBLIC.STG/secrets.csv' ; ... 1.e0from @~/x ; select 1.e5from(SECRETS) ; select 1.e0from lateral flatten(1)

   Scenario: same as review 1 finding 2. The approver sees CUSTOMERS and ORDERS, and the app reads APP_DATA.USAGE (which it owns), an unqualified table, or a staged file. A three-part or `..` name is still refused. Whether Snowflake lexes `1.e5from` as `1.e5 from` is unconfirmed, exactly as for `1from`, which review 1 blocked on. It also contradicts B-4067 (2) and SP6 design 6.1 ("a name glued to a number ... refused").
   Fix: in `_snowflake_references`, when a `.` directly follows a digit of a gap (a number) and an ident starts directly after the `.`, give the "a name directly after a number" issue, unless the whole ident is `e` followed by digits. Over-refusing every such ident is also fine. Add the forms above to TABLE_RULES_REFUSE and the render tests.

NOT BLOCKING

2. sqlrules.py:496-507 (`_ends_from`). The word after `$` or `::` is read as a clause start, so a later comma is not checked. RENDERED: `... join ORDERS o on ... and $where = 1, SECRETS s` ; `... on c.x::where, SECRETS`. I see no live use: `$where` needs a session SET (refused), and `::where` is no type. Cheap fix: require a whitespace-only gap before the word that does not follow `::` or `$`.
3. `"IDENTIFIER"('x')` outside FROM passes. It is a quoted function name, so it joins the documented function limit. In a FROM position it is refused.
4. `@s/it's ->> remove @s/o'x`: publish and run time read `'...'` as a string. Same family as #375 item 1 (quotes as well as comment marks), unchanged from main, and refused at render. Please add it to #375.
5. The run-time check still accepts `\u200bidentifier`, `1procedure(`, `1system$x(`, `system\u200b$wait` (unchanged from main). Render refuses each. Safe if Snowflake errors on them.
6. `1.e5system$wait(1)` and `system $wait(1)` render, but they are not valid calls.

FORMS FROM REVIEW 1 (run on the branch; "main" = origin/main's functions)

| # | Form | Verdict now |
|---|---|---|
| 1 | (identifier('S')) s | refused at render (table function); main passed |
| 1 | join ..., identifier('S') / table('S') | refused at render |
| 1 | sample (10), identifier / at(...), identifier | refused at render |
| 1 | (table('S') s) ; "LATERAL" CTE + lateral table('S') | refused at render |
| 1 | identifier('UDF')(1) | refused at render |
| 2 | C c, (APP_DATA.USAGE) s ; join's comma APP_DATA.USAGE | refused at render |
| 2 | (secrets) ; (CUSTOMERS, secrets) | refused at render |
| 2 | c (a,b), secrets ; x(a), ; sample(10), ; at(..), secrets | refused at render |
| 2 | forward CTE reference | refused at render |
| 2 | select *, 1from APP_DATA.USAGE | refused at render; sibling `1.e5from` is NOT (finding 1) |
| 3 | '@STG..' ; $$@STG$$ ; @~ ; @DB.S.STG | refused at render |
| 4 | @s//x ->> ... ; @s/a--b ->> ... | publish and render refuse; run time passes (#375, not worse) |
| 5 | identifier\u200b(:t\|\|'x') | refused at publish, run time, render |
| 6 | get_ddl(..) | still passes (#375, not worse) |
| 6 | a.b.nextval | table rules refuse, as on main; not tested: one- or two-part `.nextval` (documented as accepted, #375) |
| 7 | `;` + non-token text | refused at publish |
| 7 | raw :word | publish accepts, run time refuses (#375 item 3, unchanged) |

SECURITY ROUND 1 (round-1/review-security.md)

| Form | Verdict now |
|---|---|
| `from a,` *1000 and *3000 | refused; 3000 (21 KB): table rules 0.14 s, 3001 issues (was 21 s, 4.5 M); render refuses by length before any rule |
| `from(` *1500 and *8000 | refused; 8000: 0.11 s (was 15.6 s and accepted); render refuses by length |
| Near 1 MB inputs (from a, / a$$ / ''a / CTE list / identifier( ) | table rules 1.7-4.1 s, publish 1.9-5.3 s, run time at most 1.2 s; looks linear |
| (identifier('o.s.x')), (table('o.s.x')), OK o (a), table('..') | refused at render |
| (OTHER..SECRETS) ; CTE other + other..secrets | refused at render |
| '@OTHER.S.STAGE' ; @~ ; 1from ; (secrets) | refused at render |

NEW BYPASS ATTEMPTS (about 190 hand forms, plus fuzzing)
- Flow operator: after a name, number, string, `$$`, `"A"`, param or cast; newlines; `->>>`; after a comment or `//\r`. All refused at publish, run time and render. `- >>`, `->/**/>` and `'->>'` are not the operator.
- SYSTEM$/procedure: mixed case, quoted, doubled quote, dotted, after `'x'` or `$$x$$`, after `::`, BOM, zero-width after the name. All refused.
- Computed IDENTIFIER/TABLE: nested parentheses, adjacent strings, `''`, backslash, `$$`, "X", `$t`, unclosed, comments, NUL, DEL, NBSP, U+2060, U+FE0F, combining mark, `a.identifier(`. All refused at publish or render.
- data_reads at render: nested parentheses, joins inside parentheses, natural/asof join, union by name, pivot/qualify/match_recognize subqueries, lateral, directory(), values, binds, `$x`, braces, brackets, backtick, Unicode whitespace, `fro\u200dm`, recursive or nested CTEs, 4-part names, aliases equal to clause words. All refused except findings 1 and 2.
- Fuzz, 400,000 statements: Databricks differences from main 0; anything refused on main and accepted now (publish, table rules, run time) 0.
- Fuzz, publish against run time: 600,000 statements starting with `select`, 272,050 accepted at publish, 25,515 of those refused at run time (safe direction). The 12 I looked at all hold a raw `:identifier(` word (#375 item 3).

TESTS
- No test removed, renamed, skipped or xfailed. Removed assertion-area lines: the test_routes_cross_client.py:283 input (assertions intact), two input changes in test_render_snowflake.py (assertions intact), and one parametrize list in test_sqlrules_snowflake.py that only grew from 2 to 5 entries. No existing test loosened.
- The diff adds no route.

COMMANDS (exit 0 unless noted)
- git diff origin/main...HEAD (--stat, src, tests, docs)
- Probe scripts with backend\.venv\Scripts\python.exe, PYTHONDONTWRITEBYTECODE=1, from a temp directory: p1 (review forms), p2 (data_reads), p4 (finding 1), fz, fz2. p3 (flow/SYSTEM$/IDENTIFIER) exited 1 twice on my own script's string escapes, then 0.
- pytest --noconftest -n 0 on tests/build/test_sqlrules_snowflake.py, tests/build/test_sqlrules.py, tests/appgen/test_render_snowflake.py, tests/appgen/test_sqlcheck.py: 553 passed (exit code not captured; output piped through tail).
- Not run by me: database-backed tests and the full suite. round-2/checks.md says checks.sh passed at 2e47825 in 301 s.

The job worktree is unmodified (git status clean, head 2e47825) and my temp files are removed. I created no scratch worktree; `git worktree list` has one entry matching "iso2" that is not mine, and I left it alone.
