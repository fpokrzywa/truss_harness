# Core context hardening: isolation review, round 2

Branch `core-ctx` at `0dfcf2c` (fix round 1) vs `4d5b3ea`. Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: APPROVED** (round-1 items 1-2 fixed; 2 follow-ups). Probes against an archive of `0dfcf2c` as `shifde_app`, rolled back.

Fixed: (a), (b), (c) after `s.rollback()` with a savepoint open leave admin off in `session.info` and the next transaction, including `platform_admin_context` exit and the fail-closed value; whole rollback keeps the last tenant set. The `ContextSession.rollback()` mark is set/removed in `try/finally` (`core/db.py:126-136`), gone after a normal or raising rollback; a later standalone savepoint rollback still restores exactly. `with s.begin():` by exception, `close()`, `__exit__`, `reset()`, flush-failure subtransactions do not bring the snapshot back. Every Session in `src` comes from `Database.session()` (`sessionmaker(class_=ContextSession)`, db.py:261). B-114 holds; no protected test weakened.

6. FOLLOW-UP core: a bare `Session(db.engine)` still re-raises the flag on whole rollback (the hook listens on every `Session`). Restore only `if isinstance(session, ContextSession)`; add a test that `src` builds no Session outside `Database`.
7. FOLLOW-UP core (hostile only): a listener calling `rollback()` during a rollback clears the mark early; use a counter.
8. Tracked (B-703): `begin_nested` makes `after_begin` write `session.info` into the transaction; `close()` keeps `session.info`.
