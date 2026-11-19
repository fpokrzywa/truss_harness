APPROVED

# SP2-3 isolation review 1: PR #63, job 021, `origin/main...origin/factory/021-sp2-3-delivery-executor` (490570e..5795982)

(Saved by the lead from the isolation-reviewer's report; the reviewer runs read-only. Full report condensed to its findings and checks.)

No protected path touched (`backend/tests/schema/`, `core/dbrole.py`, startup role check); edits to the existing cross-client file only add lines.

Checked and correct:
- Migration `delivery_0002_executors.py`: both tables RLS ENABLED+FORCED, single `tenant_isolation` FOR ALL policy with USING and WITH CHECK on `shifde.current_tenant_id()`; composite FK `(tenant_id, executor_id)`; `UNIQUE (tenant_id, prefix)` (a collision can only come from the caller's own organization, so no cross-client hint); `shifde_app` grants column-limited, no SELECT on `secret_hash`, no DELETE, owns nothing; no ALTER DEFAULT PRIVILEGES re-adding it; tested.
- Definer `shifde.delivery_resolve_executor(text, bytea)`: pinned `search_path = pg_catalog, pg_temp`, qualified objects, EXECUTE to `shifde_app` only, no `app.*` write, returns only `(tenant_id, executor_id)`; not an oracle (needs the HMAC of the full token, hashes unreadable); revoked/expired credentials, disabled executors and non-active organizations return no row; tested.
- Executor auth and `/api/executor/v1`: bearer `shx_` only, cookies ignored, one 401 body; context set only after the resolver succeeds (`set_principal(None)`, `set_tenant`), transaction-local; `X-Executor-Version` 422 only after the token is proven; token separation tested both ways.
- Eight org-scoped routes: tenant filter in SQL plus row re-check, credential matched to its executor before rotate/revoke, `hide_unless` 404, writes need `delivery:executor:manage` and an active organization, FOR UPDATE on issue/rotate, audit never holds the secret.
- Cross-client tests: all eight routes in `ROUTE_TESTS` asserted equal to the router; foreign slug, foreign executor/credential, foreign credential under own executor all 404; lists own rows only; B's rows (including `secret_hash`) unchanged; `whoami` per organization.

Findings (none blocking):
1. `backend/tests/delivery/test_routes_cross_client.py:91-115`: the coverage table is built only from `delivery_api.router`; `delivery_gateway_api.router` is not checked. Today only `whoami`, which is covered. The next job adding a gateway route must add a second route table over the gateway router, asserted the same way, each entry proving B's executor token cannot read or change A's rows.
2. `backend/src/shifde/delivery/service.py` `authenticate_executor`: failed executor authentications are neither rate-limited nor audited (user API key failures are). Not a cross-client leak (256-bit secret under HMAC). SP2 hardening.

Allow-list entry: the exact text in DECISIONS row B-2666 is correct to add to `SECURITY_DEFINER_ALLOWLIST` in `backend/tests/schema/test_function_schema.py`, verbatim.
