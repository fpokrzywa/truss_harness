"""Ledger: rate cards, usage events and rollups, budgets, reservations, alerts, quotas.

Revision ID: ledger_0001
Revises: base_0001

SETH 3ba0e06's final schema for the cost system, folded from `p05_0001_runtime` (usage_events,
budgets, budget_reservations), `p08_0001_governance` (rate cards, rollups, alerts, quotas and the
extensions of the P5 tables) and `p08_0003_rate_card_guard` (the insert guard), plus `p06_0001_pipelines` (the `pipeline_version_id`
and `node_id` columns of usage_events).

Foreign keys to tables owned by modules that are not ported yet are NOT created here; each is owed
by the owning module's migration (see docs/build/carry_forward.md): `tenant_id -> tenants(id)` on
every table here (tenancy) and the composite `budget_reservations (tenant_id, run_id) ->
runs (tenant_id, id)` (runtime). The schema comparison lists them in `deliberate_changes.yaml`
until then. Foreign keys among the ledger's own tables are kept.

Deliberate difference from SETH (DECISIONS B-601): the rate card guard no longer signals the
immutability trigger with `set_config('shifde.ratecard_close', ...)` (the catalog scan refuses any
`set_config`); the immutability trigger recognises the guard's own nested UPDATE by
`pg_trigger_depth() > 1`. The guard pins `search_path = shifde, pg_catalog, pg_temp`. Also
B-602: `usage_events.cost_kind` admits `estimated` (a cost priced from token counts the platform
estimated because the provider reported none; R3 Item 11).

Rollback: drops the eight tables (data-destructive); back up first.
"""

from __future__ import annotations

from alembic import op

revision = "ledger_0001"
down_revision = "base_0001"
branch_labels = ("ledger",)
depends_on = None

ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"
TENANT_OR_PLATFORM = (
    "(tenant_id = shifde.current_tenant_id() "
    "OR (tenant_id IS NULL AND shifde.is_platform_admin_ctx()))"
)
EVENT_TYPES = (
    "('model_call', 'tool_call', 'run', 'pipeline_node', 'ontology_query', 'prompt_test', "
    "'build_op', 'infra_estimate')"
)
TABLES = (
    "quotas",
    "budget_alerts",
    "budget_reservations",
    "budgets",
    "usage_rollups_daily",
    "usage_events",
    "rate_card_versions",
    "rate_cards",
)


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _tenant_table(table: str) -> list[str]:
    return [
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON shifde.{table} "
        "USING (tenant_id = shifde.current_tenant_id()) "
        "WITH CHECK (tenant_id = shifde.current_tenant_id())",
    ]


def _global_or_own(table: str) -> list[str]:
    return [
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY read_global_or_own ON shifde.{table} FOR SELECT "
        "USING (tenant_id IS NULL OR tenant_id = shifde.current_tenant_id())",
        f"CREATE POLICY write_own ON shifde.{table} FOR ALL USING {TENANT_OR_PLATFORM} "
        f"WITH CHECK {TENANT_OR_PLATFORM}",
    ]


# Insert guard. (0) runs as the owner (BYPASSRLS) before the INSERT's WITH CHECK, so its first
# statement refuses anything outside the caller's tenant (or, for a global version, a caller that
# is not the platform administrator) with 42501, before any lock or lookup: no oracle on another
# tenant's cards and no owner UPDATE on rows the caller may not touch (isolation review 1,
# item 1). (1) the version belongs to the card's tenant (NULL-safe: global cards have no
# tenant). (2) a version that would close an open-ended predecessor must not start in the past
# (it would change how earlier instants resolve). (3) a new version starting AFTER an open-ended
# one supersedes it: the predecessor's NULL effective_to is closed at the successor's start (the
# only mutation the immutability trigger allows, and only from this nested UPDATE). (4) any
# remaining overlap, including a backdated version, is refused. Events are never repriced: they
# carry their own rate snapshot.
GUARD = """CREATE FUNCTION shifde.rate_card_versions_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = shifde, pg_catalog, pg_temp AS
$$
BEGIN
  IF NOT ((NEW.tenant_id IS NOT NULL AND NEW.tenant_id = shifde.current_tenant_id())
          OR (NEW.tenant_id IS NULL AND shifde.is_platform_admin_ctx())) THEN
    RAISE EXCEPTION 'rate card version is outside the caller''s tenant'
      USING ERRCODE = '42501';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('ratecard:' || NEW.rate_card_id::text, 0));
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

# Versions are immutable except the guard's NULL -> successor-start close of effective_to, which
# the guard performs from inside its own trigger (trigger depth 2). No other trigger updates
# this table and the application role holds no UPDATE grant, so depth > 1 identifies the guard.
IMMUTABLE = """CREATE FUNCTION shifde.rate_card_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS
$$
BEGIN
  IF TG_OP = 'UPDATE'
     AND pg_trigger_depth() > 1
     AND OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL
     AND NEW.effective_to > OLD.effective_from
     AND (to_jsonb(NEW) - 'effective_to') = (to_jsonb(OLD) - 'effective_to') THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'append-only table: rate card versions are immutable, add a new version'
    USING ERRCODE = 'insufficient_privilege';
END
$$"""


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ rate cards
        """CREATE TABLE shifde.rate_cards (
            id uuid PRIMARY KEY,
            tenant_id uuid,
            provider_key text NOT NULL CHECK (length(provider_key) BETWEEN 1 AND 100),
            model_key text NOT NULL CHECK (length(model_key) BETWEEN 1 AND 300),
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        "CREATE UNIQUE INDEX uq_rate_cards__scope_model ON shifde.rate_cards "
        f"(COALESCE(tenant_id, {ZERO}), provider_key, model_key)",
        *_global_or_own("rate_cards"),
        "GRANT SELECT, INSERT ON shifde.rate_cards TO shifde_app",
        """CREATE TABLE shifde.rate_card_versions (
            id uuid PRIMARY KEY,
            rate_card_id uuid NOT NULL REFERENCES shifde.rate_cards (id),
            tenant_id uuid,
            version_no int NOT NULL CHECK (version_no >= 1),
            effective_from timestamptz NOT NULL,
            effective_to timestamptz,
            currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
            unit text NOT NULL CHECK (unit IN ('per_1m_tokens', 'per_call', 'per_second')),
            input_rate numeric(24,10) NOT NULL CHECK (input_rate >= 0),
            output_rate numeric(24,10) NOT NULL CHECK (output_rate >= 0),
            cached_read_rate numeric(24,10) CHECK (cached_read_rate >= 0),
            cached_write_rate numeric(24,10) CHECK (cached_write_rate >= 0),
            other_rates jsonb NOT NULL DEFAULT '{}',
            source text NOT NULL CHECK (length(source) BETWEEN 1 AND 500),
            revision text NOT NULL CHECK (length(revision) BETWEEN 1 AND 100),
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_rate_card_versions__no UNIQUE (rate_card_id, version_no),
            CONSTRAINT ck_rate_card_versions__range
                CHECK (effective_to IS NULL OR effective_to > effective_from)
        )""",
        "CREATE INDEX ix_rate_card_versions__card ON shifde.rate_card_versions "
        "(rate_card_id, effective_from)",
        GUARD,
        "REVOKE ALL ON FUNCTION shifde.rate_card_versions_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_rate_card_versions_overlap BEFORE INSERT ON shifde.rate_card_versions "
        "FOR EACH ROW EXECUTE FUNCTION shifde.rate_card_versions_guard()",
        IMMUTABLE,
        "CREATE TRIGGER trg_rate_card_versions_immutable BEFORE UPDATE OR DELETE "
        "ON shifde.rate_card_versions FOR EACH ROW EXECUTE FUNCTION "
        "shifde.rate_card_versions_immutable()",
        *_global_or_own("rate_card_versions"),
        "GRANT SELECT, INSERT ON shifde.rate_card_versions TO shifde_app",
        # ------------------------------------------------------------------ usage events
        f"""CREATE TABLE shifde.usage_events (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            event_key text NOT NULL,
            event_type text NOT NULL CHECK (event_type IN {EVENT_TYPES}),
            status text NOT NULL DEFAULT 'succeeded' CHECK (status IN ('succeeded', 'failed')),
            workspace_id uuid,
            project_id uuid,
            principal_id uuid,
            run_id uuid,
            root_run_id uuid,
            step_id uuid,
            attempt int,
            agent_id uuid,
            agent_version_id uuid,
            prompt_version_id uuid,
            model_id uuid,
            is_test boolean NOT NULL DEFAULT false,
            input_tokens bigint NOT NULL DEFAULT 0,
            output_tokens bigint NOT NULL DEFAULT 0,
            cached_read_tokens bigint NOT NULL DEFAULT 0,
            call_count int NOT NULL DEFAULT 1,
            duration_ms int,
            provider_reported_usage jsonb NOT NULL DEFAULT '{{}}',
            cost_kind text NOT NULL DEFAULT 'unmeasured',
            pipeline_version_id uuid,
            node_id text,
            provider_key text,
            model_key text,
            cached_write_tokens bigint NOT NULL DEFAULT 0,
            prompt_id uuid,
            pipeline_id uuid,
            orchestration_id uuid,
            build_ref text,
            rate_card_version_id uuid REFERENCES shifde.rate_card_versions (id),
            rate_snapshot jsonb,
            cost_amount numeric(24,10),
            currency text,
            corrects_event_id uuid,
            CONSTRAINT uq_usage_events__key UNIQUE (tenant_id, event_key),
            CONSTRAINT usage_events_cost_kind_check CHECK
                (cost_kind IN ('provider_reported', 'calculated', 'estimated', 'estimated_infra',
                 'unmeasured')),
            CONSTRAINT ck_usage_events__cost CHECK
                ((cost_kind = 'unmeasured') = (cost_amount IS NULL) AND
                 (cost_amount IS NULL OR currency IS NOT NULL))
        )""",
        "CREATE INDEX ix_usage_events__tenant_at ON shifde.usage_events (tenant_id, occurred_at)",
        "CREATE INDEX ix_usage_events__run ON shifde.usage_events (tenant_id, root_run_id)",
        "CREATE TRIGGER trg_usage_events_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.usage_events FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
        *_tenant_table("usage_events"),
        "GRANT SELECT, INSERT ON shifde.usage_events TO shifde_app",
        "CREATE INDEX ix_usage_events__type_at ON shifde.usage_events "
        "(tenant_id, event_type, occurred_at)",
        "CREATE INDEX ix_usage_events__project_at ON shifde.usage_events "
        "(tenant_id, project_id, occurred_at)",
        "CREATE INDEX ix_usage_events__run_id ON shifde.usage_events (tenant_id, run_id)",
        # ------------------------------------------------------------------ rollups (derived)
        """CREATE TABLE shifde.usage_rollups_daily (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            day date NOT NULL,
            dim_key text NOT NULL,
            workspace_id uuid,
            project_id uuid,
            principal_id uuid,
            agent_id uuid,
            prompt_id uuid,
            model_id uuid,
            pipeline_id uuid,
            orchestration_id uuid,
            build_ref text,
            rate_card_version_id uuid,
            event_type text NOT NULL,
            cost_kind text NOT NULL,
            currency text,
            input_tokens bigint NOT NULL DEFAULT 0,
            output_tokens bigint NOT NULL DEFAULT 0,
            cached_tokens bigint NOT NULL DEFAULT 0,
            calls bigint NOT NULL DEFAULT 0,
            duration_ms bigint NOT NULL DEFAULT 0,
            cost_amount numeric(24,10) NOT NULL DEFAULT 0,
            event_count bigint NOT NULL DEFAULT 0,
            rebuilt_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_usage_rollups__dim UNIQUE (tenant_id, day, dim_key)
        )""",
        "CREATE INDEX ix_usage_rollups__day ON shifde.usage_rollups_daily (tenant_id, day)",
        *_tenant_table("usage_rollups_daily"),
        "GRANT SELECT, INSERT ON shifde.usage_rollups_daily TO shifde_app",
        "GRANT UPDATE (input_tokens, output_tokens, cached_tokens, calls, duration_ms, "
        "cost_amount, event_count, rebuilt_at, updated_at) ON shifde.usage_rollups_daily "
        "TO shifde_app",
        # ------------------------------------------------------------------ budgets
        """CREATE TABLE shifde.budgets (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            name text NOT NULL DEFAULT '',
            scope_type text NOT NULL CHECK (scope_type IN
                ('tenant', 'workspace', 'project', 'agent', 'pipeline', 'principal')),
            scope_id uuid NOT NULL,
            period text NOT NULL CHECK (period IN
                ('run', 'day', 'month', 'daily', 'monthly', 'total')),
            limit_tokens bigint CHECK (limit_tokens >= 0),
            limit_runs int CHECK (limit_runs >= 0),
            mode text NOT NULL DEFAULT 'hard' CHECK (mode = 'hard'),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            row_version int NOT NULL DEFAULT 1,
            metric text NOT NULL DEFAULT 'tokens' CHECK (metric IN ('cost', 'tokens', 'runs')),
            amount numeric(24,8) CHECK (amount >= 0),
            currency text CHECK (currency ~ '^[A-Z]{3}$'),
            enforcement text NOT NULL DEFAULT 'hard' CHECK (enforcement IN ('hard', 'soft')),
            on_exceed text NOT NULL DEFAULT 'deny' CHECK
                (on_exceed IN ('deny', 'queue_until_reset', 'require_approval')),
            alert_thresholds numeric[] NOT NULL DEFAULT '{0.5,0.8,1.0}',
            updated_at timestamptz,
            CONSTRAINT ck_budgets__run_period CHECK (period <> 'run' OR limit_runs IS NULL),
            CONSTRAINT ck_budgets__limit CHECK
                (limit_tokens IS NOT NULL OR limit_runs IS NOT NULL OR amount IS NOT NULL),
            CONSTRAINT ck_budgets__cost CHECK
                (metric <> 'cost' OR (amount IS NOT NULL AND currency IS NOT NULL)),
            CONSTRAINT uq_budgets__tenant_id UNIQUE (tenant_id, id)
        )""",
        "CREATE INDEX ix_budgets__scope ON shifde.budgets (tenant_id, scope_type, scope_id)",
        *_tenant_table("budgets"),
        "GRANT SELECT, INSERT ON shifde.budgets TO shifde_app",
        "GRANT UPDATE (name, limit_tokens, limit_runs, status, row_version) "
        "ON shifde.budgets TO shifde_app",
        "GRANT UPDATE (period, amount, currency, enforcement, on_exceed, alert_thresholds, "
        "updated_at) ON shifde.budgets TO shifde_app",
        """CREATE TABLE shifde.budget_reservations (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            run_id uuid NOT NULL,
            step_key text NOT NULL,
            budget_ids uuid[] NOT NULL,
            amount bigint NOT NULL CHECK (amount >= 0),
            settled_amount bigint,
            status text NOT NULL DEFAULT 'held' CHECK (status IN
                ('held', 'settled', 'released', 'expired')),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            cost_amount numeric(24,10) NOT NULL DEFAULT 0,
            settled_cost numeric(24,10),
            currency text,
            CONSTRAINT uq_budget_reservations__step UNIQUE (tenant_id, run_id, step_key)
        )""",
        *_tenant_table("budget_reservations"),
        "GRANT SELECT, INSERT ON shifde.budget_reservations TO shifde_app",
        "GRANT UPDATE (status, settled_amount, amount, expires_at, budget_ids) "
        "ON shifde.budget_reservations TO shifde_app",
        "GRANT UPDATE (cost_amount, settled_cost, currency) ON shifde.budget_reservations "
        "TO shifde_app",
        """CREATE TABLE shifde.budget_alerts (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            budget_id uuid NOT NULL,
            period_start timestamptz NOT NULL,
            threshold numeric(10,4) NOT NULL,
            utilization numeric(18,6) NOT NULL,
            fired_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT budget_alerts_budget_id_fkey FOREIGN KEY (tenant_id, budget_id)
                REFERENCES shifde.budgets (tenant_id, id),
            CONSTRAINT uq_budget_alerts__once
                UNIQUE (tenant_id, budget_id, period_start, threshold)
        )""",
        *_tenant_table("budget_alerts"),
        "GRANT SELECT, INSERT ON shifde.budget_alerts TO shifde_app",
        # ------------------------------------------------------------------ quotas
        """CREATE TABLE shifde.quotas (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            scope_type text NOT NULL CHECK (scope_type IN
                ('tenant', 'workspace', 'project', 'agent', 'pipeline', 'principal')),
            scope_id uuid NOT NULL,
            metric text NOT NULL CHECK (metric IN
                ('runs_per_day', 'tokens_per_day', 'concurrent_runs')),
            limit_value bigint NOT NULL CHECK (limit_value >= 0),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            row_version int NOT NULL DEFAULT 1,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_quotas__scope_metric UNIQUE (tenant_id, scope_type, scope_id, metric)
        )""",
        *_tenant_table("quotas"),
        "GRANT SELECT, INSERT ON shifde.quotas TO shifde_app",
        "GRANT UPDATE (limit_value, status, row_version, updated_at) ON shifde.quotas "
        "TO shifde_app",
    )


def downgrade() -> None:
    _run(
        *[f"DROP TABLE shifde.{t}" for t in TABLES],
        "DROP FUNCTION shifde.rate_card_versions_guard()",
        "DROP FUNCTION shifde.rate_card_versions_immutable()",
    )
