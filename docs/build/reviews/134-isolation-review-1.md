# Job 134 (#267, PR #276) isolation review 1: APPROVED

Reviewer: `isolation-reviewer` (opus), read-only, diff `origin/main...origin/factory/134-runtime-read-only-sql`; report written by the lead.

1. Scope: no route, service, migration, RLS policy or connection lookup changes; three pure check functions and a module move (`build/sqlrules.py` -> `core/sqlrules.py`, docstring only).
2. Refusal messages are fixed strings and never echo SQL (`adapters/postgres.py:58-63`, `adapters/dataplatforms/sqlapi.py:41-46`; asserted in `tests/adapters/test_postgres_read_only.py:57-64`).
3. Cross-client: `tests/connections/test_read_only_invoke.py:84-108`: A's refusal, invocation status and audit rows hold nothing of B; B gets 404 on A's connection under both org paths, with no audit rows; refusals are 422 with a `capability` failure audit.
4. No write bypass found on the read path (Postgres write keywords and `into` anywhere, unterminated or nested comments, backslash in strings, non-positional `$` refused before `_connect`; READ ONLY transaction stays the backstop. Databricks/Snowflake write keywords anywhere outside quotes and comments).

Pre-existing, not cross-client, not blockers: 5. side-effecting Postgres functions in a READ ONLY transaction (`pg_terminate_backend`, `pg_advisory_lock`, `set_config`): the target role is the control. 6. Snowflake `SYSTEM$...` functions pass as one word: the read-only credential (B-1507) is the control.
