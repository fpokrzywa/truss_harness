# Job 138 (PR #304, SP8-4 Fabric adapter) isolation review 1

Reviewer: isolation-reviewer (opus), 2026-11-23, head `b6e9efb`. Written by the lead from the reviewer's reply.

**APPROVED** (adapter only; `registry.py:35` still maps `fabric` to `planned:FabricPlanned`, so nothing calls it).

## Holds
- Artifact, plan, receipt refused for another tenant or target before any request (`adapter.py:124, 285, 647`; `planning.py:28`); owned-resource and foreign-item refusals (`adapter.py:345-349, 297-304, 371-375`); readback checked against workspace, type, name.
- Workspace binding is a lowercase GUID; host is the Fabric API origin; redirects off, proxies ignored, LRO Locations never fetched, job Locations allow-listed, 24 MiB streaming cap; errors carry only status; token scope fixed; 12 isolation tests.

## Findings (lead: 1-3 fixed in round 2 before merge)
1. `adapter.py:86,132`: `PLATFORMS = ("fabric","fake")` accepts a fake receipt in any environment; production accepts only `fabric`.
2. `clients.py:88`: Entra response 64 KiB check after full read; stream and cap.
3. `backend/pyproject.toml:69`: ruff exclusion covers the whole templates folder; narrow to `*.ipynb`.
4. `fake.py:72`: fake ships in `src` with a placeholder token; unreachable today.

## Preconditions for the wiring PR (SP8-5, #290)
A. One `EntraTokenProvider` per organization connection, looked up from `target.tenant_id` as `shifde_app` under RLS, never shared or cached across organizations.
B. Each organization its own Entra tenant and service principal; no shared principal.
C. Target's Entra tenant and workspace set only by that organization's authorized users, checked against its connection; changes bump `target.revision`.
D. `spec` for observe/verify comes from the sealed artifact behind the stored receipt; owned resources and journal from RLS rows.
E. Fake only with `SHIFDE_DEV_FAKE_DATA_PLATFORMS=true` in development or test; production refuses `platform="fake"`.
F. Cross-client 404 tests for every new route; Fabric errors do not reveal another organization's workspace or item.
