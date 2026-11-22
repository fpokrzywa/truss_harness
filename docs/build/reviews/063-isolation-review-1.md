APPROVED. Diff origin/main...f3bb5b9 (PR #151, job 063), isolation review 1 (isolation-reviewer, saved by the lead).

Scope: backend/src/shifde/executor/{credentials.py,main.py,runner.py}, executor and delivery tests, scripts/{delivery,release}_demo.py, docs. No migration, RLS schema test, startup role check, backend/tests/schema, backend/tests/tenancy, factory/ or .claude/ change; no gateway or route code.

Verified:
1. credentials.py:44,103: `tenant_id: UUID` is a required field (extra="forbid"); an entry without it or with a non-UUID makes the whole file `CredentialFileRefused` (no fallback). `entry_for` refuses `entry.tenant_id != tenant_id` with the same fixed `CredentialUnavailable` as an unknown reference (no existence oracle).
2. runner.py:343-349, 361-369: both broker paths pass the leased target's `tenant_id` (initial `entry_for` and lazy `_token_provider.token_for`, target a `model_copy` of `ctx.target`). The new `ctx.job.id != job.id` check runs before `credential_grant`.
3. Tests: test_credentials.py adds other-tenant refusal and no-tenant_id / non-UUID malformed cases; test_runner.py adds a foreign-tenant entry on the same host and reference (job fails `credential_unavailable`, no platform call) and a context naming another job (no grant, no platform call); test_gateway_cross_client.py adds A and B sharing an application_key with A's /context listing only A's resources and B's rows unchanged; test_end_to_end.py gives runners A and B their own tenants' files. No assertion removed or loosened.
4. DECISIONS.md: B-2738 moved below the new B-2737 row, identical text (reorder only).

Non-blocking (filed as a follow-up issue):
a. runner.py:369 compares `ctx.target.tenant_id` as the gateway reports it: defence in depth, not a replacement for the gateway's organization enforcement. Follow-up (deferred in B-2737): tenant on /whoami, pinned at executor startup, compared with every context and file entry.
b. runner.py:361: the context's plan and deployment `tenant_id` are not checked against `ctx.target.tenant_id`.
c. credentials.py accepts a file mixing entries of different organizations; "one organization per file" is documented but not enforced.
