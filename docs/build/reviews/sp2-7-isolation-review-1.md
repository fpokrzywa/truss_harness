APPROVED (isolation-reviewer, opus) — PR #73, origin/factory/025-sp2-7-shifde-executor at 3138780 vs origin/main.

No way found for one organization's executor to get another's target, plan, receipt, native resources, credential or artifact.
1. Context route (gateway.py:241-309, gateway_api.py:103-114): _leased first; job read with executor tenant + explicit tenant check -> NotFound identical to unknown id; then executor id, lease id, fence, expiry. Target/plan/deployment read with executor tenant using ids from the locked job row.
2. Response (contracts.py:398-438) built field by field; never credential_ref, claim or artifact bytes; host has no userinfo; test_context.py:104 asserts credential_ref absent.
3. list_native_resources (repository.py:1047-1062) filters tenant_id, target_id, application_key, plus RLS.
4. Cross-client: route in GATEWAY_ROUTE_TESTS and _calls, count guard 8; test_b_cannot_reach_as_job: 404 identical to unknown, A's job and journal unchanged. Also in test_routes.py and test_workspace_routes.py.
5. Import-linter (pyproject.toml:113,164-188): renamed contract keeps source and forbidden list; new shifde.executor forbidden contract; layers only add shifde.executor beside shifde.main. Nothing relaxed.
6. Executor (runner.py:358-369,340-356; credentials.py:83-101): refuses context whose target.id != leased job target_id; broker hands token only for grant naming this job/executor/target, unexpired, audience == normalized host; per-call host check; grants per job; errors name no secret.

Non-blocking (filed as follow-up):
7. credentials.py:39-101: credentials file has no organization binding; a file shared across organizations' executors could let B's target copy A's credential_ref and host. Add tenant_id to CredentialEntry, check it in entry_for; document one file per organization.
8. runner.py:360: also assert ctx.job.id == lease.job.id (defensive).
9. test_gateway_cross_client.py: add a positive-content test that A's context never lists B's owned_resources with a shared application_key.
