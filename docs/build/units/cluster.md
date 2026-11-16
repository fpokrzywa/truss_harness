# Unit cluster: notes

Work is finished and committed on `unit/cluster` (rebased onto origin/main `c132ac4`, head `3df437b`). The unit is **not merged**: it is blocked on a Next.js advisory stop.

## State
- Local cluster is running on 55450. All 290 `shifdet_*` databases are dropped. See S-007 in DECISIONS.
- `scripts/dev_db.py start` waits for crash recovery with `pg_isready`, with 3 new tests. Script suite: 43 passed.
- STATUS row, S-007 and the `units.json` `cluster` entry (`done`) are already in the commit.

## Why the merge did not land
`python scripts/gates.py --ref 3df437b... --name cluster-merge` gives 12/13, run twice. The failing gate is `pnpm build`: it compiles and type-checks, then Next.js 16.4.4 aborts with `SecurityFatalError` (known vulnerability, advisory fetched from the npm registry). The nudge carries a fresh id on every clean checkout, so a rerun does not clear it. Every unit's gate will hit it. A retry in the same checkout exits 0, but that is not a clean-checkout gate pass.

## Exact next step
1. The supervisor lands the `nextjs-security` unit first. The merge lock was released for it.
2. Then: `git fetch; git rebase origin/main` (union-resolve `DECISIONS.md` and `units.json`, keep every row).
3. `python scripts/unit.py lock merge --unit cluster`, then `python scripts/gates.py --ref <sha> --name cluster-merge`. All 13 must pass.
4. `git push origin HEAD:main`, `unit.py unlock merge --unit cluster`, `unit.py done cluster` (must print UNIT DONE).
