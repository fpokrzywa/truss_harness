# Isolation review 1, job 173 (#286 SP8-1, PR #354) at 8d09a9f

BLOCKED (isolation-reviewer, 2026-11-24; code read, Palantir normalizer run; test suites not run).

1. BLOCKING. `backend/src/shifde/adapters/native/addresses.py:213-227` (`normalize_palantir_address`, suffix check at 225) accepts multi-label hosts; spec requirement 4 and the docstring say `https://<label>.palantirfoundry.com` or `<label>.<extra suffix>`. `https://eu.acme.palantirfoundry.com` is accepted and pinned as correct by `tests/adapters/native/test_addresses.py:187`. A claims `acme.palantirfoundry.com`; B registers `x.acme.palantirfoundry.com`, gets another claim key and 201 instead of the fixed 409; if the stack answers below its host (not checked live), two clients hold one stack. Fix: for some suffix, the part before `"." + suffix` must be non-empty with no dot, else `PalantirAddressRefused`; a deeper site domain is listed in `SHIFDE_DELIVERY_PALANTIR_HOST_SUFFIXES`. Tests: move `eu.acme` to the refusals; an extra-suffix two-label refusal; a 422 in `BAD_ADDRESSES["palantir"]` (`tests/delivery/test_targets.py`); `test_platform_claims.py`: B's sub-host form of A's stack writes nothing.
2. Not blocking (SP8-9, #294): `DockerBindings.workspace_host` may name a Databricks workspace another client claims; #294 must enforce `workspace_not_claimed` before any plan.
3. Not blocking: an extra Palantir suffix that is a vanity alias of a `palantirfoundry.com` stack gives two claim keys; say so at `core/settings.py:218-220` or in a runbook.

No finding on: Fabric and Docker keys, the `[`/`]` refusal (only narrows), 409 text, claim definer, both migrations (CHECKs only), executor dispatch and credentials, packaging refusals, tests (nothing loosened).
