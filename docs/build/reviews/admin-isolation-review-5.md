# admin isolation review 5 (part 3b: secret and audit routes, `secrets_0002`, follow-ups): APPROVED

Diff `ae02c75..9d9f100`. Reviewer: `pytest -n 4 tests/admin tests/secrets tests/schema tests/identity/test_platform_groups.py tests/tenancy/test_owner_rules.py` 303 passed; scratch worktree and database removed.

Schema test not loosened: no rule or matching logic in `test_rls_schema.py` changed. `PLAIN_FOREIGN_KEYS` swaps the dropped `secret_access_log_secret_id_fkey` for a plain key that accepts strictly less (installation rows can name only installation secrets); `UNIQUE_WITHOUT_TENANT` gains `uq_secrets__installation_secret_id` (NULL on client rows, a copy of the PK on installation rows); seven `deliberate_changes.yaml` entries each name one exact path.

Checked and sound: `secrets_0002` (client rows checked only by the composite key; B's, installation and unknown ids fail on the same constraint; generated columns and `tenant_id` not writable; no DELETE); `resolve` refuses a session/PDP client mismatch before lookup with the unknown-id answer and no log row; `create_secret` one `SECRET_NAME_CONFLICT`; six routes check `secret:manage` before lookup, keyed by client, B's/installation secret like missing, B's scope 422 like unknown, audit list/verify per client (B's forged chain leaves A ok), values absent from responses, logs, audit rows; `oidc.py:488` principal restored in `finally`, single own-row read; sync count rows carry no actor, IP, user agent; re-enable 409 then version-conditioned; `bind_group_as_platform_admin` re-checks inside the write; API-key mutators filter on `tenant_id`; `list_idps` cursor inside context; `ROUTE_TESTS` covers all six.

Non-blocking follow-ups:
1. `test_rls_schema.py:1375,1424`: exemptions keyed by name only; pin `pg_get_constraintdef` for the two keys and the generation expressions of both `installation_secret_id` columns (pre-existing weakness, not new loosening).
2. `oidc.py:488-495` `_directory_person`: on an aborted transaction the `finally` restore raises over the original error; restore as `forget_tenant` does.
3. `oidc.py:513`: count rows carry `trace_id`/`correlation_id` equal to the sign-in request id across bound clients; drop or mint per client if no correlation key is wanted.
