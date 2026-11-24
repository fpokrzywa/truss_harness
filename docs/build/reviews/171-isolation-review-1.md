# Isolation review 1: job 171 (#347, PR #349, head f966289)

APPROVED (isolation-reviewer, 2026-11-24; recorded by the lead, the reviewer is read-only).

1. runtime_0009 (lines 80-91): both retention lookups filter `p.tenant_id = OLD.tenant_id` and are read through RLS; PRIMARY KEY (tenant_id, data_class) keeps each subquery to one row; SECURITY INVOKER, fixed search_path, qualified relations; triggers, owner, grants and PUBLIC revoke unchanged.
2. The rule matches `effective_days` with no `--older-than`; pins tie FLOOR_DAYS and the 90/90 defaults to RETENTION_FLOORS/RETENTION_DEFAULTS.
3. Downgrade exact: PREVIOUS_GUARD pinned to runtime_0008's GUARD; scratch-database test checks body, owner, config, secdef, PUBLIC and triggers at 0008 and 0007.
4-6. Cutoffs moved 30 -> 100 days keep every condition under test (OLD 200 days, default 90); the cross-client test still deletes 0 rows with B's rows intact; the floor test stores retain_days = floor so the floor stays the bound.
7. RLS schema test and startup role check untouched; test_schema.py only moves the runtime head to runtime_0009.
8. Cutoff from `SELECT now()` in the client's own transaction (maintenance.py:242), tested; no cross-client effect.
9. The nine proposed reason texts are the current text plus one accurate runtime_0009 sentence; they declare exactly the intended difference.
10. (Applied by the lead) the run_artifacts append-only reason now names runtime_0009.
11. Not new, not cross-client: shifde_app can update its own client's retention_policies through the governance path; the guard stops cutoffs inside stored retention, not an authorized change of it.
