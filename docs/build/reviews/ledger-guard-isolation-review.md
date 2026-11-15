# ledger guard fix isolation review (`73115e0..5f1d9b2`, B-611): APPROVED

Reviewer (same as governance review 1, which found it): `pytest -n 4 tests/ledger tests/governance tests/schema tests/core` 429 passed at `5f1d9b2`.

1. Oracle closed: the first step of `rate_card_versions_guard` (`ledger_0002`) is `COALESCE(..., false)` over "client row in its own context" or "global row with the platform-admin flag"; everything else gets one 42501 before any lock, lookup or update. The parametrized test gets one answer for B's card, an unknown id and a future start, with no context and in A's context.
2. Lock closed: the advisory lock is taken only after the check; a probe with `lock_timeout` 500ms while B holds its card lock gets 42501, not 55P03.
3. No other step reachable early: the rest of the body is byte-identical to `ledger_0001`; owner, grants, SECURITY DEFINER kept, `search_path` restated; downgrade restores the old body.
4. Legitimate paths intact: client inserts in their own context, platform-admin global inserts, `_seed_context` seeding; the moved SETH guard test's assertion unchanged. Only change: the owner with no client context can no longer insert a client version (no production path does).
5. Migration graph: one more head, fine with `alembic upgrade heads`.

Non-blocking: wrap the >100-character comment line added to `ledger_0001_ledger.py` (~line 88).
