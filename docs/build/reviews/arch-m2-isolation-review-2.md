# ARCH M2 (job 012, #10) isolation review 2 (round-2 delta d76e2ec..6f7df37): APPROVED

1. `connections/repository.py:299-329`: capped loop keeps conflict target `(tenant_id, kind, dedupe_key)`; `_active_test_job` read-back filters `tenant_id`, kind, dedupe_key; after 3 misses `ConnectionTestBusy`; request rolled back, test asserts nothing left.
2. `core/errors.py:90-93`: 409 `connection_test_busy`, fixed message, no ids or other-client state.
3. `connections/service.py:1149-1157`: after `IntegrityError` + rollback, `repository.get_test(session, test_id, tenant_id=tenant_id)` filters `id` and `tenant_id` (from the claimed job row) under RLS.
4. Client context after rollback: `set_config(..., true)` re-applied by the `after_begin` hook from `session.info`; `platform_admin` stays false; missing context fails closed (re-raise, nothing written).
5. An unexplained `IntegrityError` is re-raised.
6. Tests only added (`test_test_jobs.py`); no assertion removed; nothing under `tests/schema`, startup role check, migrations or grants changed.
7. Round 1 note 8 now recorded (B-2611..B-2613).
