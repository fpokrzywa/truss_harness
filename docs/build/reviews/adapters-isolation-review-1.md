# Adapters: isolation review, round 1

Branch `adapters` at `82cf9cb` vs main `46c236c`. Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: APPROVED.** `tests/schema tests/core tests/adapters -n 8`: 413 passed.

1. Protected tests not weakened: `test_skeleton.py:21-29` keeps `versions == set(code_heads())`, ancestry loop is stronger (optional: `assert versions`, owner core); role-check tests `:227-243`, `:291-292` compute the owned relation with the same filter as `dbrole.py`, still require `UnsafeDatabaseRole` and the exact text; `test_rls_schema.py:554-569` scratch fixture drops global tables only for checker probes, the real-database test (`:513`) still covers listed tables (docstring at `:557` names a non-existent test: nit).
2. `adapters_0001` matches SETH `p03_0001` (forced RLS, SELECT-only `read_all`, SELECT grant, append-only trigger); downgrade correct.
3. `adapter_conformance_runs` holds no client content on any shipped path (`run_suite` called only by owner CLI; suites target local test servers, sample DB or Anthropic; `environment` = Python version, platform).
4. Secret-scan markers (`suites.py:66,180,486`) mark SETH's dummy test-server tokens; mechanism pre-exists in `scan-secrets.py`.
5. No `app.*` context, client table, engine or module cache in adapters; conformance uses `ExecContext(tenant_id=None)`.

## Follow-up

- vendors/cli port: SETH `vendors/service.py:66-90` returns `checks`/`environment` to every client; `PostgresSuite` connects to `SHIFDE_SAMPLE_DATABASE_URL` and could record a client DB's table names/SQL in `checks.detail` if pointed at one. Refuse non-sample URLs in the CLI, or drop `detail` from the client-facing API.
- vendors/connections ports: `ExecContext.tenant_id` (`base.py:116`) must carry the organization.
