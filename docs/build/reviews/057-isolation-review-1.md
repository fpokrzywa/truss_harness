# 057 (PR #138, issue #64) isolation review 1 — APPROVED

Reviewer: isolation-reviewer (opus), 2026-11-21, on `factory/057-sp2-gateway-hardening-cross` head `5a10f27` (commits `159b2a5`, `5a10f27`) vs origin/main. Read-only; no tests run by the reviewer.

1. The `delivery.executor.token.rejected` row (`delivery/service.py:603-618, 645-647`) is written only when `resolve_executor` returns nothing (every refusal reason), so no organization is known; it holds fixed reason `invalid`, `auth_method`, outcome, `actor_ip`, no token material, executor id or organization; filed through the installation path of `audit.record` (`audit/service.py:262-265`); principal and organization setting cleared first. Tested `tests/delivery/test_executor_auth.py:314-353`.
2. The commit inside `require_executor` (`delivery/gateway_api.py:40-64`) holds only the resolver read and the audit insert; the 401 follows, no route body runs on that session. Success path unchanged.
3. The per-IP limit (`gateway_api.py:47-51`) runs before the Authorization header is parsed; over the limit no resolver call and no audit row; a valid token also gets 429 (`test_executor_auth.py:356-374`).
4. Forwarded headers: reuses `RequestContext.client_ip` / `ip_limit_applies` (`core/ratelimit.py:74-94, 107-137`); `X-Forwarded-For` believed only from a trusted proxy or loopback peer. Optional: a test on the executor route with a remote peer plus `X-Forwarded-For: 127.0.0.1` expecting 429 (follow-up #142).
5. No test removed or weakened; `tests/delivery/test_routes_cross_client.py` docstring only; `GATEWAY_ROUTE_TESTS` unchanged; no migration, RLS policy, grant, definer function or startup role check touched.
6. Executor scope unchanged: organization only from the resolver result (`service.py:661-663`).
7. Not isolation: concurrent failures can overrun the window by the number of parallel requests; loopback and trusted-proxy peers without a forwarded header are exempt by design and audited.
