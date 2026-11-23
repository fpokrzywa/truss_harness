# 141 (#268, PR #307) isolation review 1: APPROVED (2026-11-23, isolation-reviewer opus)
- adapter_key/connection_type only from the connection row re-read under the caller's org (connections/service.py:1389, build/tool_runtime.py:218); cross-client tests test_snowflake_sql_cross_client.py:56,:99,:164.
- Values sent only as SQL API bindings; types limited to BIGINT/DOUBLE/BOOLEAN/DATE/STRING; positional_sql literal reading matches publish (sqlrules.py:866); runtime check_read_only(snowflake) on the ? form.
- Fake sample_data off by default, test-only.
- (a) typed columns: no consumer breaks (ontology engine/pipeline accept only `database` connections; console shows JSON). Record shape difference in DECISIONS.
- (b) `call` already refused at publish; SELECT/WITH required. Usability gap only.
- (c) replaced test not a weakening (test_databricks_tool_validation.py:1132).
Non-blocking: sqlrules.py:926 Snowflake reading skips `:<digit>`; refuse as sql_positional_parameter (follow-up).
