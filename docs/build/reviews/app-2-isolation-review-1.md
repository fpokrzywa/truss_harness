# app-2 isolation review 1 (part 2a, d03febd..3a9ac10)

Verdict: APPROVED (opus isolation-reviewer, 2026-11-16).

- Runtime router mounted behind `require_auth`; every route under `/api/v1/orgs/{org}` resolves the client through `ScopeDep`/`resolve_org`.
- `app.state.governance = TokenBudgetHooks()` (alias of `PolicyBudgetHooks`, stateless).
- `GET /api/v1/platform/status`: `_require_platform_admin` first (unpinned user session with PDP `platform:admin`); 403 body carries no data; reads only the installation-wide `workers` table as `shifde_app`.
- `installation.service._table_exists` removal only drops an existence probe.
- Tests extended: runtime cross-client cases run on the minimal and the mounted app; mounted-route coverage test; platform-status route case in `ROUTE_TESTS`.

Non-blocking notes: 7 `oidc_providers` hard-coded 0 (closed, B-2441); 8 add pinned-session and API-key 403 cases (closed, B-2442); 9 confirm both route variants pass (confirmed by the 2a and lead runs).
