# Connections part 2 (service, routes, C2): isolation review 2

Diff `e70a23f..04440d9` (branch `connections`; head `8cd03b8` adds a merge of main with no connections code). Reviewer: isolation-reviewer (opus), 2026-11-14. Saved by the lead from the reviewer's report.

**Verdict: APPROVED** (0 blocking, 3 follow-up). Clean detached worktree: `tests/connections tests/vendors tests/secrets tests/schema -n 6` 286 passed; two probes as `shifde_app` through the route harness on a scratch database, dropped afterwards.

## What holds

- **Client only from `resolve_org`:** every route takes its client from `ScopeDep`; B's slug gets the unknown-slug 404; nothing reads a client from the path or a header.
- **Every lookup keyed to the caller's client** (connections, tests, discoveries, egress policies, secrets, vendors) as well as RLS; for B's ids all 16 routes give the unknown-id 404 body; own object first in each test; B's rows and counts compared unchanged.
- **B's secret, policy and private vendor are refused** on create and PATCH with the unknown-id 404 (probed for the vendor).
- **Read-only confirmation (B-1507)** required for a credential on a `read` connection at create, on secret change, and on `read_write` -> `read`; recorded in audit details.
- **Egress guard:** `build_guard` refuses unless `current_tenant_id()` equals its client; `ExecContext.tenant_id`, the guard and `resolve_system(tenant_id=)` come from the connection row; `_own` refuses another client's row handed in by `open_trusted`/`prepare_call`.
- **`test_egress.py` is stricter** (RuntimeError instead of accepting a deny-all guard under the wrong client; new test for no-client and other-client contexts).
- **C2 holds (charter section 5 item 3 met):** a destination change while a secret stays bound needs `secret:use` on that secret; detach plus repoint sends nothing, and rebinding afterwards needs `secret:use` (404 when hidden), with deliveries counted at a second local listener; every adapter's destination fields are tested with a drift guard; Host/Authorization are forbidden in `default_headers`, `join_url` refuses absolute and `//` paths, postgres pins `hostaddr`.
- **`_check_secret` (B-1508):** a secret the caller cannot see gets "secret not found", same as unknown.
- **`ROUTE_TESTS` equals the router's route set.**

## Follow-ups (owner: connections unless noted)

1. **C2 race** (recommended next). `service.py:439-501`: `_get_row` takes no lock and a PATCH without `row_version` updates without a version check (`repository.py:91`). A manager without `secret:use` PATCHes `base_url` on a secretless connection while the owner binds secret S between the manager's read and write: the host-change check is skipped, and `/test` then sends S to the new host (SETH has the same gap). Fix: pass `body.row_version if body.row_version is not None else row["row_version"]` to `update_connection` in `update` and `set_status` (or `SELECT ... FOR UPDATE` for writes); test interleaving the two PATCHes.
2. **Existence oracle across workspaces inside one client.** GET, `/tests`, `/discoveries/latest` (`service.py:376,697,763`, `hide=None`) answer 403 for a connection in a workspace the caller cannot read, 404 "connection not found" for unknown; `/test`, `/invoke`, PATCH, `/status`, `/discover` answer 404 "resource not found". Fix: re-raise `NotFound("connection not found")` in `_require`, `hide="connection:read"` on those three; same-404 cross-workspace test.
3. **Test claim too strong.** `tests/connections/test_routes_cross_client.py:229-230`: A's and B's connections share one `rest_server`, so "never used by A" proves nothing (refusal still shown by the identical 404 and unchanged `_b_state`). Fix: B gets its own listener, or assert the request count equals A's own calls.

Note (owner: adapters): with `allow_redirects` on an egress policy, httpx strips `Authorization` on a cross-origin redirect but not the `api_key` header or `default_headers`.
