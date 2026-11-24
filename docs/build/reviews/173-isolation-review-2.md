# Isolation review 2, job 173 (#286 SP8-1, PR #354) at 69e4312

APPROVED (isolation-reviewer, 2026-11-24; base origin/main cbf6ab4; code read, address normalizers run on hostile inputs, test suites not run). Saved by the lead from the reviewer's report. Since the merge ca1e0c4 only addresses.py, settings.py (comment) and three test files change.

1. Review 1 finding 1 (blocking) FIXED: `backend/src/shifde/adapters/native/addresses.py:227` accepts exactly one non-empty label before a suffix. Tests at `tests/adapters/native/test_addresses.py:205-211, 227`, `tests/delivery/test_targets.py:558`, `tests/delivery/test_platform_claims.py:685-706`; nothing loosened.
2. Note 3 FIXED (`backend/src/shifde/core/settings.py:218-222`).
3. Note 2 stays with #294: `DockerBindings.workspace_host` is not claim-checked; not reachable today.
4. New, NOT BLOCKING (operator configuration only): an extra Palantir suffix nested under another suffix reopens finding 1 (`SHIFDE_DELIVERY_PALANTIR_HOST_SUFFIXES=acme.palantirfoundry.com`, or `foundry.corp.example` plus `stack-1.foundry.corp.example`). Fix by SP8-7 (#292), before any Palantir plan can run: `_valid_host_suffixes` (`core/settings.py:293-303`) refuses a Palantir entry below another entry or below `palantirfoundry.com` at any depth; at minimum a sentence in the comment or runbook.

No finding on claims, both migrations (CHECKs only; counts per client under forced RLS on downgrade), executor dispatch and credentials, packaging refusals, plans and gateway, cross-client tests. Only protected-path files: the two migrations.
