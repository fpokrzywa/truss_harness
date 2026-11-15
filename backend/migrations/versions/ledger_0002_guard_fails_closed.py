"""Ledger: the rate card version guard fails closed without a client context.

Revision ID: ledger_0002
Revises: ledger_0001

Governance isolation review 1, item 1 (DECISIONS B-611). `rate_card_versions_guard` (SECURITY
DEFINER, BEFORE INSERT, so it runs before the INSERT's WITH CHECK) opened with
`IF NOT ((NEW.tenant_id IS NOT NULL AND NEW.tenant_id = shifde.current_tenant_id()) OR ...)`.
With no client context `current_tenant_id()` is NULL, the condition is NULL and `IF` skips the
refusal: the definer then took the card's advisory lock, read the card, closed its open-ended
version and ran the overlap check before WITH CHECK refused the row. A caller with no context
naming another client's card got one of three errors (overlap / past start, "does not match its
rate card", the RLS violation), which told that client's card ids and version ranges apart, and
queued on (or held) that client's lock.

The function is replaced with the same signature, owner, grants (CREATE OR REPLACE keeps them),
`search_path` and body except the first step: the refusal is two-valued. A client row
(`NEW.tenant_id IS NOT NULL`) needs `current_tenant_id() IS NOT DISTINCT FROM NEW.tenant_id`; a
global row (NULL tenant) needs the platform-administrator context, as before; anything else gets
the one 42501 answer, before any lock, lookup or update, whatever the card id. The outer
`COALESCE(..., false)` keeps the step closed should a helper ever answer NULL.

Rollback: restores the ledger_0001 body (the gap returns).
"""

from __future__ import annotations

from alembic import op

revision = "ledger_0002"
down_revision = "ledger_0001"
branch_labels = None
depends_on = None

_HEAD = """CREATE OR REPLACE FUNCTION shifde.rate_card_versions_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = shifde, pg_catalog, pg_temp AS
$$
BEGIN
"""

_FAIL_CLOSED = """  IF NOT COALESCE(
       (NEW.tenant_id IS NOT NULL
        AND shifde.current_tenant_id() IS NOT DISTINCT FROM NEW.tenant_id)
       OR (NEW.tenant_id IS NULL AND shifde.is_platform_admin_ctx()),
       false) THEN
    RAISE EXCEPTION 'rate card version is outside the caller''s tenant'
      USING ERRCODE = '42501';
  END IF;
"""

_OPEN = """  IF NOT ((NEW.tenant_id IS NOT NULL AND NEW.tenant_id = shifde.current_tenant_id())
          OR (NEW.tenant_id IS NULL AND shifde.is_platform_admin_ctx())) THEN
    RAISE EXCEPTION 'rate card version is outside the caller''s tenant'
      USING ERRCODE = '42501';
  END IF;
"""

# Steps (1)-(4) of ledger_0001, unchanged: the version belongs to the card's tenant; a version
# that would close an open-ended predecessor must not start in the past; a later version closes
# the open-ended one at its start (the immutability trigger allows only this nested UPDATE); any
# remaining overlap is refused.
_REST = """  PERFORM pg_advisory_xact_lock(hashtextextended('ratecard:' || NEW.rate_card_id::text, 0));
  IF NOT EXISTS (
    SELECT 1 FROM shifde.rate_cards c
    WHERE c.id = NEW.rate_card_id AND c.tenant_id IS NOT DISTINCT FROM NEW.tenant_id
  ) THEN
    RAISE EXCEPTION 'rate card version tenant does not match its rate card'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.effective_from < now() AND EXISTS (
    SELECT 1 FROM shifde.rate_card_versions v
    WHERE v.rate_card_id = NEW.rate_card_id AND v.effective_to IS NULL
      AND v.effective_from < NEW.effective_from
  ) THEN
    RAISE EXCEPTION 'a new rate card version cannot start in the past: closing the open-ended '
      'version would rewrite how earlier instants resolve; start it now or later'
      USING ERRCODE = '23P01';
  END IF;
  UPDATE shifde.rate_card_versions v SET effective_to = NEW.effective_from
  WHERE v.rate_card_id = NEW.rate_card_id AND v.effective_to IS NULL
    AND v.effective_from < NEW.effective_from;
  IF EXISTS (
    SELECT 1 FROM shifde.rate_card_versions v
    WHERE v.rate_card_id = NEW.rate_card_id
      AND tstzrange(v.effective_from, COALESCE(v.effective_to, 'infinity'), '[)')
          && tstzrange(NEW.effective_from, COALESCE(NEW.effective_to, 'infinity'), '[)')
  ) THEN
    RAISE EXCEPTION 'rate card version overlaps an existing effective range'
      USING ERRCODE = '23P01';
  END IF;
  RETURN NEW;
END
$$"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_HEAD + _FAIL_CLOSED + _REST)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_HEAD + _OPEN + _REST)
