# Isolation review 1: job 031 (SP3-2 compatibility check and sealed releases, PR #92, head 978a9209)

Reviewer: `isolation-reviewer` (opus), 2026-11-20, diff only. Filed by the lead from the agent's reply.

**APPROVED.** No way for one client to read, seal, withdraw or pin another client's releases, artifact versions, connections or models, or learn of them.

Checked: `packaging_0002` (`releases` RLS enabled + forced, one `tenant_isolation` policy USING + WITH CHECK, composite FK to solution_versions ON DELETE RESTRICT, `UNIQUE (tenant_id, digest)`, `shifde_app` SELECT, INSERT, UPDATE(status) only; both functions SECURITY INVOKER, `search_path = pg_catalog, pg_temp`, EXECUTE revoked from PUBLIC; retire guard keyed on `r.tenant_id = NEW.tenant_id`); `deliberate_changes.yaml:752-756` (lead, B-2530) declares exactly the one trigger added; `packaging/repository.py` RELEASE_SELECT tenant-keyed joins; `_release` one 404 for unknown/foreign/unreadable, PDP with `hide_unless=READ`; `own_model(shared=False)` refuses shared SHI, foreign and unknown alike (call 15); `conns.placement` one answer; `build/repository.py:375 pipeline_resource_refs` tenant-keyed; `tests/packaging/test_routes_cross_client.py` covers every route incl. the 4 new ones (byte-identical answers, B's rows unchanged); all 4 SCOPED in `test_workspace_routes.py:548-551`.

029 review findings closed: 1 (hidden pins redacted, `test_releases.py:599`), 2 (`in_scope` at create and seal, `:677`), 4 (pipeline model/connection refs read, shared refused, `:435`, `:362`), 5 (W1 user / W2 agent, `test_routes_cross_client.py:321`).

Non-blocking:
1. `packaging/service.py:701`: `evaluation.dataset_id` still unvalidated (029 finding 3); already in issue #80.
2. `service.py:712-741` `_release_out` (within one client): manifest `bindings`, `data_reads` and `models.<id>` / `connections.<id>` report paths not redacted for pins/connections the reader cannot see; same in the refused-seal report and the unknown-binding check (`:573`). Follow-up issue.
3. The yaml reason and migration docstring say the retire guard refuses the owner; the guard reads `releases` under forced RLS, so the owner without a client context sees none and the retire passes. Not a leak; wording or guard to be corrected. Follow-up issue.

Security review (job round 1) non-blocking notes, carried to the same follow-up: every IntegrityError in build `set_state` maps to the 409 "a sealed release pins this version"; refused seals each commit an audit row with no rate limit.
