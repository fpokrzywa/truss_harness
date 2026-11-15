# runtime isolation review 3 (RT-4 `context.py`, `settle.py`, `gate.py`, `run_of`; `git diff 8b1cdef 43f6745`)

Reviewer: `isolation-reviewer` (opus), 2026-11-15. Read-only; saved by the lead.

**Verdict: APPROVED.** No path to another client's data. Finding 1 must be fixed before the runtime merge; 2-4 Low.

Checked: `tests/runtime` + `tests/ledger/test_findings.py` at 43f6745 in `.worktrees/verify3`: 223 passed. Builder's proof rerun in memory only: all client predicates removed (63 in `runtime/repository.py`, 32 in `build/repository.py`): 12/14 owner cases fail; with `_run_env` client comparisons also removed 14/14; removing the `only_versions` check in `_Env.version` fails the pinned-version test; removing the `artifact_id` check in `_run_env` fails the another-artifact refusal. `run_of` cannot be met with another client's run, an unknown run or a run of another artifact: `_run_env` (`build/service.py:668-686`) re-reads the run with the client id, takes the version from the run's own target column, requires the version's tenant, artifact and kind to match `art`; `pin_closure` keyed by `tenant_id`; pins have composite keys; `version()` re-reads with the client id. The unchecked env is built only in the validated `approval_of` path (`:666`) and `_run_env` (`:686`), reached by the runtime only via `RunContext.env` (`context.py:118`). C1 settle cannot act across clients: `settle_terminal`, `wake_parent`, `cancel_open_children` key every call by the run's own client; the audit row uses the run's tenant; forged-id cases leave B's rows unchanged. Review 2 findings 1-3 closed (B-2124).

## Findings

1. **Medium (not cross-client; before merge).** The "`latest()` answers nothing" guard test is vacuous: `tests/runtime/test_execution.py:239`, guard `build/service.py:1295`; `tests/build/env_support.py::add_published` never sets `artifacts.latest_published_version_id`, so `latest()` is None without the guard (guard removed: all 4 env tests passed). Without the guard a run's env could reach the latest version of any artifact of the client in any workspace or project, unchecked. Fix: seed `latest_published_version_id`, assert `latest(a.agent)` and `latest(<unpinned prompt artifact>)` are None.
2. **Low.** `_Env.permits` (`build/service.py:1385-1402`) answers True for any of the client's artifacts in a run env. Fix: also require `self.only_versions is None or ref.version_id in self.only_versions`.
3. **Low.** `_run_env` trusts the caller's `art` mapping (only `id`, `tenant_id`, `kind` checked; `workspace_id`/`project_id` feed `model_problems` via `scope_ref_of(self.art)`, `:1310`). Fix: re-read with `get_artifact(tenant, art["id"])`.
4. **Low (RT-5/RT-7).** `prepare(..., run_of=)` for an ontology validates the draft's mappings (`:722`), not the pinned version's. Fix: use the pinned version's mappings or refuse `run_of` for ontologies until RT-5.

## Follow-ups

- RT-6/RT-7: in a run env `_readable_connection`, `connection`, `schema_discovery`, `route_model_ids` (`:1317-1364`) reach every connection and route of the client (no `connection:read`, `usable_by_publisher=True`); limit to what the pinned specs name and the run's workspace/project (B-2121 defers).
- RT-6: `ledger.record` and `runtime/repository.py::insert_usage_event` (`:1095`) still accept a caller `provider_key`; only `record_model_call` refuses one.
- RT-10/RT-11: B-2124, B-2125 items stand (`lease_job_id`, scope tuples, `cancel_open_children` callers).

Findings 1-4 handed to the RT-5 builder (same branch).
