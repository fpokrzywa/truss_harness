# Isolation review 1: job 169 (#271, PR #348, head d52135e)

APPROVED (isolation-reviewer, 2026-11-24; recorded by the lead, the reviewer is read-only). Branch commits 336e29b..d52135e.

1. `packaging_0004` downgrade (lines 60-82) mirrors delivery_0007: reads the client list with the platform-administrator setting, counts each client's Snowflake solutions under that client's own setting, clears both (transaction-local); runs as the schema owner via Alembic; the error names only the total count. `tests/packaging/test_target_kind_db.py:113-134` proves it across two clients. The upgrade changes only a CHECK; no policy, grant, owner or definer change.
2. `core/sqlrules.py:220`: a Snowflake `:name` token now carries `start`/`end`. Only reader is `appgen/service.py` `sql_template`. Tokenizing otherwise unchanged; table allow-list still checked in the connection's dialect; tool still bound to the request's own connection id.
3. New connection lookups in `appgen/service.py` use the organization-scoped `ConnectionService.get_row` on the request's own connection id; sealing keeps the caller-readable and in-scope checks.
4. No test weakened: the only removed assertion is the renamed code `connection_not_databricks` -> `connection_platform_unsupported`. RLS schema test and startup role check untouched; `tests/identity/test_schema.py` only adds the head. New cross-client tests both ways in `tests/appgen/test_routes_cross_client.py` and `tests/packaging/test_routes_cross_client.py`.
