# models_admin isolation review 3 (part 2b, `ba349a6..a543c0a`): APPROVED

Reviewer: `pytest -n 4 tests/models_admin tests/ledger tests/schema` 314 passed at `a543c0a`, plus three probes (switch flipped between lookup and call: second gate refused, fake SHI server saw no request, refusal audited; B's workspace/project/tenant ids as route or allowlist scope answered like unknown; SHI key and base URL absent from response, audit, usage and model tables). Scratch worktree removed.

Checked and sound: 8 new `{org}` routes through `resolve_org`, every repository and limiter call keyed by `sc.tenant_id`, `shared` required; allowlist members, route primary and every fallback limited to the client's own models (unknown-model 422 before any key); SHI gate at lookup and again before the adapter call, SHI hidden from `effective_models`/`problems_at` with the switch off, refusal row names only the caller's own scope; SHI key is a masked, redaction-registered `SecretStr` read only in `_shi_call`, client egress can only narrow; shared SHI models priced from global cards only (B-1734); budgets, buckets, leases per client; `record_model_call_durably` under the caller's client; `models_admin_0003` and YAML correct; `shared=True` test edits weaken nothing; B-1735 (owner trusted for `changed_by`) acceptable. Review 1 items 2, 6, 7 closed, 3 accepted (B-1737); review 2 items 1-4 closed.

Non-blocking follow-ups:
1. `service.py:952-960` vs `:1033`: SHI-gate audit row written before `pdp.require("model:invoke")`: a member without `model:read` can flood denied rows; with the switch on, unauthorised callers get "resource not found" for a SHI id vs "model not found" for unknown. Check permission first, same 404.
2. `service.py:1056`: `max_tokens` not capped at `max_output_tokens`; with per-client buckets one client can drain SHI's shared upstream quota (availability only, B-1737). Cap it; platform-level bucket.
3. `service.py:590-611`: a lock timeout in `record_model_call_durably` after the provider answered writes no usage row and returns 500 (under-counts own budget). Fall back to `record_model_call` in the request transaction.
4. `service.py:676-679`, `:837-839`: `_read`/`_manage` without `hide_unless`: hidden workspace in the same org 403 vs unknown 404 (same org, as SETH).
5. `core/settings.py:186`: `shi_provider_config` not validated against the adapter `config_schema`; a secret put there would not be masked.
6. Tests to add: second gate after a switch flip; key and base URL absent from response/audit/usage; B's workspace/project as route scope and B's project as allowlist scope.
7. B-1735: note that any future admin CLI writing `shi_provider_access` sets `changed_by` explicitly.
