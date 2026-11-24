APPROVED

Job 151 (#315, PR #322), branch factory/151-deadlock-concurrent-evaluation-case at aa837ac vs origin/main. Isolation-reviewer verdict, written to disk by the lead from the agent's report (the agent was read-only).

1. backend/src/shifde/secrets/repository.py:61-94 (`touch_secret`): the locking subselect is keyed on `Secret.id == secret_id` and `_owned_by(tenant_id)`, runs as `shifde_app` in the caller's organization session, so RLS still applies. At most one row (primary key). When nothing is selected (locked, other organization, or touched within the interval) the UPDATE matches `id = NULL` and changes nothing. A skipped lock skips only this secret's row; no write can land on another client's row.
2. backend/src/shifde/secrets/service.py:469-472 (`_touch`): uses `secret.id` and `secret.tenant_id` from the row `get_secret` already fetched with the tenant filter (and, in `resolve`, the organization and PDP check, lines 353-360). No new caller input.
3. service.py:363-374, 387-399: the `secret_access_log` insert and `audit.record` still run on every read, in the same transaction, before the touch. A skipped or throttled touch never drops an audit event or access-log row; on failure all three roll back together. Only the lock order changed.
4. No schema, policy, grant or route changes; no SECURITY DEFINER function or owner-role engine in a request path. Tests in backend/tests/secrets/test_concurrent_reads.py use `owner_session` only to check results.
5. Optional hardening (non-blocking): no test calls `touch_secret` with organization B's id (or `tenant_id=None`) for organization A's secret and checks it changes nothing. Filed as a follow-up.
