# Job 139 (PR #302, SP8-6 Palantir Marketplace adapter) isolation review 1

Reviewer: isolation-reviewer (opus), 2026-11-23, head `925ff0e`. Written by the lead from the reviewer's reply.

**APPROVED.** No route, table, policy, query or worker path; nothing in `backend/src` builds the adapter or the fake yet (unreachable today).

## Holds
- Artifact, plan, receipt and reconcile checked against caller tenant and target (`adapter.py:456-467, 500-501, 611-612, 698-699`); apply also checks plan vs artifact, toolchain, journal intent and snapshot fingerprint.
- No adoption by name: `_owned` (`adapter.py:353-369`) and `_check_installation`; installation RID re-derived from a host-prefixed binding (`receipts.py:1220-1229`).
- RIDs checkpointed before the next request; writes never replayed.
- Token minted at `target.host` with `follow_redirects=False`, `trust_env=False`, one client per operation; secret hidden from `repr`; error bodies not echoed.
- `frontend_origin` GET carries no headers (tested `test_palantir.py:275`), no redirects, 2 MiB cap, HTTPS only; only a hash match comes back.
- Databricks spec guard covers every entry point, both directions tested.

## Preconditions for the wiring PR (SP8-7, #292)
1. `clients.py:754, 817`: credential provider keyed only by host; wiring must check the entry's `tenant_id` against the job's organization (plus grant audience, job, executor, target) as `executor/credentials.py:98-131` does.
2. `fake.py:1000-1006`: build the fake only behind the executor's `--fake-platform` with `SHIFDE_ENV` development or test, like `FakePlatform`.
3. `clients.py:909-914, 783-792`: no `core/ssrf.py`; fine inside the organization's own executor, but route both URLs through `core/ssrf.py` with a pinned IP if this client ever runs in the shared API or worker.
4. Informational: `frontend_origin` may equal `target.host`; that GET still carries no credentials.
