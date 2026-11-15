"""Runtime: runs, steps, events, artifacts, tool invocations, approvals, jobs, workers.

Revision ID: runtime_0001
Revises: base_0001 (branch `runtime`)

SETH 3ba0e06's final schema for the runtime tables, folded from `p05_0001_runtime` (tables, guards,
queue functions), `p05_0003_run_lease_job` (`runs.lease_job_id`), `p06_0001_pipelines` (pipeline
runs, `pipeline_node` steps and approvals), `p06_0003_run_pipeline_integrity`
(`fk_runs__pipeline_of_version`), `p07_0001_ontology` (ontology-action runs), `p08_0001_governance`
(`workers.last_reap_at`, `shifde_ops_stats(int)`) and `p11_0001_unknown_write_outcome`
(`tool_invocations.arguments`, `land_verdicts`, `unknown_outcome_text`, `settle_ended_run`, the
final `runs_guard` and `shifde_reap_expired_leases`). The permission keys of `p05_0002` /
`p06_0002` are seeded by `authz_0001`; `usage_events`, `budgets`, `budget_reservations` by
`ledger_0001`.

* Every client table: ENABLE + FORCE row-level security, SETH's `tenant_isolation` policy, column
  UPDATE grants only, no DELETE grant. `workers` (heartbeat rows: host, pid, version) and
  `run_state_transitions` (the legal status changes) hold no client data and are listed in
  `tests/schema/global_tables.txt`.
* Foreign keys deferred to this module by earlier ones: `fk_budget_reservations__run` (ledger),
  `fk_ontology_lineage__run` (ontology); `runs` -> `artifact_versions` (build) as in SETH.
* Deliberate differences from SETH (`tests/schema/deliberate_changes.yaml`, DECISIONS B-2101,
  B-2102): unique keys that omit `tenant_id` start with it (`uq_run_events__seq`,
  `uq_run_steps__attempt`); client-keyed composite foreign keys where SETH had a bare id column
  (`approvals` workspace, project and tool version; `run_steps` child run, tool invocation and
  tool version; `runs.root_run_id`), with `uq_tool_invocations__tenant_id_id` as the target.
* SECURITY DEFINER (owner rights, every client): `shifde_claim_jobs`, `shifde_reap_expired_leases`
  (the worker's queue and reaper) and `shifde_ops_stats` (aggregates only). Each pins
  `search_path = pg_catalog, pg_temp`, names every relation schema-qualified, is executable by
  `shifde_app` only and refuses (42501) inside a client context (`app.tenant_id` set): the worker
  calls them with no client context and sets the job's own client before it touches the run
  (DECISIONS B-2103). `settle_ended_run`, `land_verdicts`, `unknown_outcome_text` run with the
  caller's rights (row-level security applies under `shifde_app`); the reaper calls
  `settle_ended_run` with the run's own id.

Rollback: drops every runtime table and function (data-destructive for run data) and the two
foreign keys added to ledger and ontology tables.
"""

from __future__ import annotations

from alembic import op

revision = "runtime_0001"
down_revision = "base_0001"
branch_labels = ("runtime",)
depends_on = ("ledger_0002", "ontology_0001")

TERMINAL = "('completed', 'failed', 'cancelled', 'timed_out')"
STATUSES = (
    "('pending', 'running', 'waiting_for_approval', 'completed', 'failed', 'cancelled', "
    "'timed_out')"
)
TRANSITIONS = [
    ("pending", "running"),
    ("pending", "cancelled"),
    ("pending", "failed"),
    ("pending", "timed_out"),
    ("running", "waiting_for_approval"),
    ("running", "pending"),
    ("running", "completed"),
    ("running", "failed"),
    ("running", "cancelled"),
    ("running", "timed_out"),
    ("waiting_for_approval", "pending"),
    ("waiting_for_approval", "failed"),
    ("waiting_for_approval", "cancelled"),
    ("waiting_for_approval", "timed_out"),
]
DROP_ORDER = [
    "approvals",
    "run_artifacts",
    "run_events",
    "run_steps",
    "tool_invocations",
    "jobs",
    "runs",
    "workers",
    "run_state_transitions",
]

# The queue functions refuse inside a client context (B-2103): they read and write every client's
# jobs and runs, so only the worker's context-free transaction may call them.
NO_CLIENT_CONTEXT = """  IF shifde.current_tenant_id() IS NOT NULL THEN
    RAISE EXCEPTION 'not available inside a client context' USING ERRCODE = '42501';
  END IF;"""


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


def _event(tenant: str, run: str, kind: str, frm: str, to: str, detail: str) -> str:
    return (
        "INSERT INTO shifde.run_events (id, tenant_id, run_id, seq, at, type, from_status, "
        f"to_status, details) VALUES (pg_catalog.gen_random_uuid(), {tenant}, {run}, v_seq, "
        f"pg_catalog.now(), '{kind}', {frm}, {to}, {detail}::jsonb);"
    )


RUNS_GUARD = f"""CREATE FUNCTION shifde.runs_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS
$$ BEGIN
  IF OLD.status IN {TERMINAL} THEN
    IF to_jsonb(NEW) - 'event_seq' = to_jsonb(OLD) - 'event_seq' THEN
      RETURN NEW;
    END IF;
    IF OLD.status <> 'completed' AND NEW.error_class = 'indeterminate_side_effect'
       AND to_jsonb(NEW) - 'event_seq' - 'error_class' - 'error_message'
           = to_jsonb(OLD) - 'event_seq' - 'error_class' - 'error_message' THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION 'run is in a terminal state and immutable' USING ERRCODE = '23514';
  END IF;
  IF NEW.status <> OLD.status THEN
    IF NOT EXISTS (SELECT 1 FROM shifde.run_state_transitions t
                    WHERE t.from_status = OLD.status AND t.to_status = NEW.status) THEN
      RAISE EXCEPTION 'illegal run transition %% -> %%', OLD.status, NEW.status
        USING ERRCODE = '23514';
    END IF;
    IF NEW.state_version <= OLD.state_version THEN
      RAISE EXCEPTION 'a status change must advance state_version' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$"""

APPROVALS_GUARD = """CREATE FUNCTION shifde.approvals_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS
$$ BEGIN
  IF OLD.status <> 'pending' THEN
    RAISE EXCEPTION 'a decided approval is final' USING ERRCODE = '23514';
  END IF;
  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.run_id <> OLD.run_id
     OR NEW.arguments IS DISTINCT FROM OLD.arguments THEN
    RAISE EXCEPTION 'approval identity columns are immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$"""

CLAIM = f"""CREATE FUNCTION shifde.shifde_claim_jobs(
    p_worker text, p_max int, p_lease_seconds int, p_tenant_max int DEFAULT 0)
RETURNS TABLE (job_id uuid, tenant_id uuid, kind text, run_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS
$$
#variable_conflict use_column
BEGIN
{NO_CLIENT_CONTEXT}
  RETURN QUERY
  WITH candidates AS (
    SELECT j.id FROM shifde.jobs j
     WHERE j.status = 'queued' AND j.run_after <= now()
       AND (p_tenant_max <= 0 OR
            (SELECT count(*) FROM shifde.jobs x
              WHERE x.tenant_id = j.tenant_id AND x.status = 'leased') < p_tenant_max)
     ORDER BY j.priority, j.run_after, j.id
       FOR UPDATE SKIP LOCKED
     LIMIT GREATEST(p_max, 0)
  ), claimed AS (
    UPDATE shifde.jobs j
       SET status = 'leased', lease_owner = p_worker, heartbeat_at = now(),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           attempts = j.attempts + 1
      FROM candidates c WHERE j.id = c.id
    RETURNING j.id, j.tenant_id, j.kind, j.run_id
  )
  SELECT claimed.id, claimed.tenant_id, claimed.kind, claimed.run_id FROM claimed;
END $$"""

LAND = r"""CREATE FUNCTION shifde.land_verdicts(p_run uuid) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS
$$
#variable_conflict use_column
DECLARE
  v_tenant uuid;
BEGIN
  SELECT tenant_id INTO v_tenant FROM shifde.runs WHERE id = p_run;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  -- An operator's verdict (`mark_succeeded` / `fail`) on a call lands on every earlier delivery
  -- of the call that is still unknown (epoch <= the decision's).
  WITH done AS (
    UPDATE shifde.tool_invocations ti
       SET status = CASE v.decision WHEN 'mark_succeeded' THEN 'resolved_succeeded'
                                    ELSE 'resolved_failed' END,
           finished_at = now()
      FROM (SELECT DISTINCT ON (a.call_ref) a.call_ref, a.epoch, a.decision
              FROM shifde.approvals a
             WHERE a.run_id = p_run AND a.tenant_id = v_tenant
               AND a.reason = 'indeterminate_side_effect' AND a.status IN ('approved', 'rejected')
               AND a.decision IN ('mark_succeeded', 'fail')
             ORDER BY a.call_ref, a.epoch DESC) v
     WHERE ti.run_id = p_run AND ti.tenant_id = v_tenant AND ti.call_ref = v.call_ref
       AND ti.epoch <= v.epoch AND ti.status IN ('indeterminate', 'resolved_retry')
    RETURNING ti.id AS inv_id, v.decision AS decision)
  UPDATE shifde.run_steps st
     SET status = CASE d.decision WHEN 'mark_succeeded' THEN 'succeeded' ELSE 'failed' END,
         error_class = CASE d.decision WHEN 'mark_succeeded' THEN NULL
                                       ELSE 'indeterminate_side_effect' END,
         error_message = CASE d.decision
               WHEN 'mark_succeeded' THEN 'an operator confirmed the write succeeded'
               ELSE 'an operator declared the write failed or unverifiable' END,
         finished_at = now()
    FROM done d
   WHERE st.run_id = p_run AND st.tenant_id = v_tenant AND st.tool_invocation_id = d.inv_id
     AND st.status = 'indeterminate';
  UPDATE shifde.run_steps st
     SET status = CASE v.decision WHEN 'mark_succeeded' THEN 'succeeded' ELSE 'failed' END,
         error_class = CASE v.decision WHEN 'mark_succeeded' THEN NULL
                                       ELSE 'indeterminate_side_effect' END,
         error_message = CASE v.decision
               WHEN 'mark_succeeded' THEN 'an operator confirmed the write succeeded'
               ELSE 'an operator declared the write failed or unverifiable' END,
         finished_at = now()
    FROM (SELECT DISTINCT ON (a.call_ref) a.call_ref, a.epoch, a.decision
            FROM shifde.approvals a
           WHERE a.run_id = p_run AND a.tenant_id = v_tenant
             AND a.reason = 'indeterminate_side_effect' AND a.status IN ('approved', 'rejected')
             AND a.decision IN ('mark_succeeded', 'fail')
           ORDER BY a.call_ref, a.epoch DESC) v
   WHERE st.run_id = p_run AND st.tenant_id = v_tenant AND st.kind = 'pipeline_node'
     AND st.idempotency_key IS NOT NULL AND st.status = 'indeterminate'
     AND 'node:' || st.node_id = v.call_ref AND st.attempt - 1 <= v.epoch;
END $$"""

TEXT = r"""CREATE FUNCTION shifde.unknown_outcome_text(p_run uuid, p_cause text) RETURNS text
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS
$$
DECLARE
  v_tenant uuid;
  v_open int;
  v_names text;
  v_cause text := p_cause;
BEGIN
  -- Every lookup names the run's own client (B-2101; isolation review 1, F4): the reaper calls
  -- this as the schema owner, which row-level security does not restrict.
  SELECT tenant_id INTO v_tenant FROM shifde.runs WHERE id = p_run;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  SELECT count(*),
         string_agg(DISTINCT COALESCE(a.tool_key, a.call_ref), ', '
                    ORDER BY COALESCE(a.tool_key, a.call_ref))
    INTO v_open, v_names
    FROM shifde.approvals a
   WHERE a.run_id = p_run AND a.tenant_id = v_tenant
     AND a.reason = 'indeterminate_side_effect' AND a.status = 'pending';
  IF v_open = 0 THEN
    RETURN NULL;
  END IF;
  IF v_cause IS NULL THEN
    SELECT COALESCE(details->>'previous_message', details->>'previous_error_class')
      INTO v_cause FROM shifde.run_events
     WHERE run_id = p_run AND tenant_id = v_tenant AND type = 'outcome_unknown'
     ORDER BY seq LIMIT 1;
  END IF;
  IF v_cause IS NULL THEN
    SELECT status INTO v_cause FROM shifde.runs WHERE id = p_run AND tenant_id = v_tenant;
  END IF;
  RETURN 'a write may have been delivered before this run ended (' || v_cause
         || '); its outcome is unknown until an operator resolves '
         || CASE WHEN v_open = 1 THEN 'the approval' ELSE 'the ' || v_open || ' approvals' END
         || ' (' || v_names || ')';
END $$"""

_APPROVAL_COLUMNS = (
    "id, tenant_id, run_id, workspace_id, project_id, step_seq, call_ref, epoch, reason, "
    "tool_version_id, tool_key, tool_version_no, side_effect_class, arguments, context_summary, "
    "required_permission, requested_by, expires_at"
)
_NODE_APPROVAL_COLUMNS = (
    "id, tenant_id, run_id, workspace_id, project_id, step_seq, call_ref, epoch, reason, "
    "side_effect_class, arguments, context_summary, required_permission, requested_by, expires_at"
)
_WRITES = "('write_idempotent', 'write_non_idempotent', 'destructive')"

SETTLE = f"""CREATE FUNCTION shifde.settle_ended_run(p_run uuid) RETURNS integer
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS
$$
#variable_conflict use_column
DECLARE
  r record;
  i record;
  s record;
  v_step uuid;
  v_stepseq int;
  v_stepstatus text;
  v_open int;
  v_new int := 0;
  v_n int;
  v_seq int;
  v_next int;
  v_cause text;
  v_note constant text := 'the run ended while this write was in flight; whether it was delivered is unknown';
BEGIN
  SELECT * INTO r FROM shifde.runs WHERE id = p_run;
  IF NOT FOUND OR r.status NOT IN {TERMINAL} THEN
    RETURN 0;
  END IF;

  IF r.status <> 'completed' THEN
    -- 0. verdicts already given (the run ended before a resume applied them)
    PERFORM shifde.land_verdicts(p_run);
    --    a decided `retry` was consumed by nothing: the delivery it covers stays unknown
    UPDATE shifde.tool_invocations ti
       SET status = 'resolved_retry', finished_at = now()
      FROM shifde.approvals a
     WHERE ti.run_id = p_run AND ti.tenant_id = r.tenant_id AND ti.status = 'indeterminate'
       AND a.run_id = ti.run_id AND a.tenant_id = ti.tenant_id AND a.call_ref = ti.call_ref
       AND a.epoch = ti.epoch AND a.reason = 'indeterminate_side_effect'
       AND a.status = 'approved' AND a.decision = 'retry';

    -- 1. write calls journaled as started (write-ahead) and never answered, and calls already
    --    indeterminate whose decision was never opened
    FOR i IN SELECT * FROM shifde.tool_invocations ti
              WHERE ti.run_id = p_run AND ti.tenant_id = r.tenant_id
                AND ti.side_effect_class IN {_WRITES}
                AND (ti.status = 'started'
                     OR (ti.status = 'indeterminate'
                         AND NOT EXISTS (SELECT 1 FROM shifde.approvals q
                                          WHERE q.run_id = ti.run_id AND q.tenant_id = ti.tenant_id
                                            AND q.call_ref = ti.call_ref AND q.epoch = ti.epoch
                                            AND q.reason = 'indeterminate_side_effect')))
    LOOP
      IF i.status = 'started' THEN
        UPDATE shifde.tool_invocations
           SET status = 'indeterminate', error_class = 'indeterminate_side_effect',
               finished_at = now()
         WHERE id = i.id AND tenant_id = r.tenant_id;
      END IF;
      v_step := NULL; v_stepseq := NULL; v_stepstatus := NULL;
      SELECT id, seq, status INTO v_step, v_stepseq, v_stepstatus FROM shifde.run_steps
       WHERE run_id = p_run AND tenant_id = r.tenant_id AND tool_invocation_id = i.id
       ORDER BY attempt DESC LIMIT 1;
      IF v_stepstatus IN ('running', 'failed') THEN
        UPDATE shifde.run_steps
           SET status = 'indeterminate', error_class = 'indeterminate_side_effect',
               error_message = v_note, finished_at = now()
         WHERE id = v_step AND tenant_id = r.tenant_id;
      END IF;
      INSERT INTO shifde.approvals ({_APPROVAL_COLUMNS})
      SELECT gen_random_uuid(), r.tenant_id, r.id, r.workspace_id, r.project_id, v_stepseq,
             i.call_ref, i.epoch, 'indeterminate_side_effect', i.tool_version_id, a.key,
             av.version_no, i.side_effect_class,
             COALESCE(i.arguments, (SELECT p.arguments FROM shifde.approvals p
                        WHERE p.run_id = r.id AND p.tenant_id = r.tenant_id
                          AND p.call_ref = i.call_ref
                          AND p.reason <> 'indeterminate_side_effect'
                        ORDER BY p.epoch DESC, p.requested_at DESC LIMIT 1), '{{}}'::jsonb),
             '{{"after_run_end": true}}'::jsonb, 'approval:decide', r.initiated_by_principal_id,
             now() + interval '24 hours'
        FROM shifde.artifact_versions av
        JOIN shifde.artifacts a ON a.id = av.artifact_id AND a.tenant_id = av.tenant_id
       WHERE av.id = i.tool_version_id AND av.tenant_id = r.tenant_id
      ON CONFLICT (tenant_id, run_id, call_ref, reason, epoch) DO NOTHING;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_new := v_new + v_n;
    END LOOP;

    -- 2. pipeline write nodes in flight: the step carries the idempotency key written ahead of
    --    the call; the epoch is attempt - 1
    FOR s IN SELECT * FROM shifde.run_steps st
              WHERE st.run_id = p_run AND st.tenant_id = r.tenant_id AND st.kind = 'pipeline_node'
                AND st.idempotency_key IS NOT NULL
                AND (st.status = 'running'
                     OR (st.status = 'indeterminate'
                         AND NOT EXISTS (SELECT 1 FROM shifde.approvals q
                                          WHERE q.run_id = st.run_id AND q.tenant_id = st.tenant_id
                                            AND q.call_ref = 'node:' || st.node_id
                                            AND q.epoch = st.attempt - 1
                                            AND q.reason = 'indeterminate_side_effect')))
    LOOP
      IF s.status = 'running' THEN
        UPDATE shifde.run_steps
           SET status = 'indeterminate', error_class = 'indeterminate_side_effect',
               error_message = v_note, finished_at = now()
         WHERE id = s.id AND tenant_id = r.tenant_id;
      END IF;
      INSERT INTO shifde.approvals ({_NODE_APPROVAL_COLUMNS})
      VALUES (gen_random_uuid(), r.tenant_id, r.id, r.workspace_id, r.project_id, s.seq,
              'node:' || s.node_id, s.attempt - 1, 'indeterminate_side_effect',
              'write_non_idempotent',
              jsonb_build_object('node', s.node_id, 'type', split_part(s.name, ':', 1)),
              jsonb_build_object('node', s.node_id, 'after_run_end', true), 'approval:decide',
              r.initiated_by_principal_id, now() + interval '24 hours')
      ON CONFLICT (tenant_id, run_id, call_ref, reason, epoch) DO NOTHING;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_new := v_new + v_n;
    END LOOP;

    -- 2b. a call still open (an unknown delivery behind a decided `retry`, no later success, no
    --     verdict) with no decision waiting: ask again, at the next free epoch
    FOR i IN SELECT DISTINCT ON (ti.call_ref) ti.* FROM shifde.tool_invocations ti
              WHERE ti.run_id = p_run AND ti.tenant_id = r.tenant_id
                AND ti.status = 'resolved_retry'
                AND ti.side_effect_class IN {_WRITES}
                AND NOT EXISTS (SELECT 1 FROM shifde.tool_invocations n
                                 WHERE n.run_id = ti.run_id AND n.tenant_id = ti.tenant_id
                                   AND n.call_ref = ti.call_ref
                                   AND n.epoch > ti.epoch AND n.status = 'succeeded')
                AND NOT EXISTS (SELECT 1 FROM shifde.approvals q
                                 WHERE q.run_id = ti.run_id AND q.tenant_id = ti.tenant_id
                                   AND q.call_ref = ti.call_ref
                                   AND q.reason = 'indeterminate_side_effect'
                                   AND q.status = 'pending')
              ORDER BY ti.call_ref, ti.epoch DESC
    LOOP
      v_stepseq := NULL;
      SELECT seq INTO v_stepseq FROM shifde.run_steps
       WHERE run_id = p_run AND tenant_id = r.tenant_id AND tool_invocation_id = i.id
       ORDER BY attempt DESC LIMIT 1;
      SELECT GREATEST(
               COALESCE((SELECT max(x.epoch) FROM shifde.tool_invocations x
                          WHERE x.run_id = i.run_id AND x.tenant_id = r.tenant_id
                            AND x.call_ref = i.call_ref), -1),
               COALESCE((SELECT max(x.epoch) FROM shifde.approvals x
                          WHERE x.run_id = i.run_id AND x.tenant_id = r.tenant_id
                            AND x.call_ref = i.call_ref), -1)) + 1
        INTO v_next;
      INSERT INTO shifde.approvals ({_APPROVAL_COLUMNS})
      SELECT gen_random_uuid(), r.tenant_id, r.id, r.workspace_id, r.project_id, v_stepseq,
             i.call_ref, v_next, 'indeterminate_side_effect', i.tool_version_id, a.key,
             av.version_no, i.side_effect_class,
             COALESCE(i.arguments, '{{}}'::jsonb),
             '{{"after_run_end": true}}'::jsonb, 'approval:decide',
             r.initiated_by_principal_id, now() + interval '24 hours'
        FROM shifde.artifact_versions av
        JOIN shifde.artifacts a ON a.id = av.artifact_id AND a.tenant_id = av.tenant_id
       WHERE av.id = i.tool_version_id AND av.tenant_id = r.tenant_id
      ON CONFLICT (tenant_id, run_id, call_ref, reason, epoch) DO NOTHING;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_new := v_new + v_n;
    END LOOP;
    FOR s IN SELECT DISTINCT ON (st.node_id) st.* FROM shifde.run_steps st
              WHERE st.run_id = p_run AND st.tenant_id = r.tenant_id AND st.kind = 'pipeline_node'
                AND st.idempotency_key IS NOT NULL AND st.status = 'indeterminate'
                AND EXISTS (SELECT 1 FROM shifde.approvals q
                             WHERE q.run_id = st.run_id AND q.tenant_id = st.tenant_id
                               AND q.call_ref = 'node:' || st.node_id
                               AND q.epoch = st.attempt - 1
                               AND q.reason = 'indeterminate_side_effect'
                               AND q.status = 'approved' AND q.decision = 'retry')
                AND NOT EXISTS (SELECT 1 FROM shifde.run_steps n
                                 WHERE n.run_id = st.run_id AND n.tenant_id = st.tenant_id
                                   AND n.node_id = st.node_id AND n.kind = 'pipeline_node'
                                   AND n.attempt > st.attempt AND n.status = 'succeeded')
                AND NOT EXISTS (SELECT 1 FROM shifde.approvals q
                                 WHERE q.run_id = st.run_id AND q.tenant_id = st.tenant_id
                                   AND q.call_ref = 'node:' || st.node_id
                                   AND q.reason = 'indeterminate_side_effect'
                                   AND q.status = 'pending')
              ORDER BY st.node_id, st.attempt DESC
    LOOP
      SELECT GREATEST(
               COALESCE((SELECT max(x.attempt) FROM shifde.run_steps x
                          WHERE x.run_id = s.run_id AND x.tenant_id = r.tenant_id
                            AND x.node_id = s.node_id), 0),
               COALESCE((SELECT max(x.epoch) + 1 FROM shifde.approvals x
                          WHERE x.run_id = s.run_id AND x.tenant_id = r.tenant_id
                            AND x.call_ref = 'node:' || s.node_id), 0))
        INTO v_next;
      INSERT INTO shifde.approvals ({_NODE_APPROVAL_COLUMNS})
      VALUES (gen_random_uuid(), r.tenant_id, r.id, r.workspace_id, r.project_id, s.seq,
              'node:' || s.node_id, v_next, 'indeterminate_side_effect',
              'write_non_idempotent',
              jsonb_build_object('node', s.node_id, 'type', split_part(s.name, ':', 1)),
              jsonb_build_object('node', s.node_id, 'after_run_end', true), 'approval:decide',
              r.initiated_by_principal_id, now() + interval '24 hours')
      ON CONFLICT (tenant_id, run_id, call_ref, reason, epoch) DO NOTHING;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_new := v_new + v_n;
    END LOOP;
  END IF;

  -- 3. everything else that must not outlive the run. An unknown-outcome approval does.
  UPDATE shifde.approvals SET status = 'cancelled'
   WHERE run_id = p_run AND tenant_id = r.tenant_id AND status = 'pending'
     AND reason <> 'indeterminate_side_effect';
  UPDATE shifde.budget_reservations SET status = 'released'
   WHERE run_id = p_run AND tenant_id = r.tenant_id AND status = 'held';
  IF r.status <> 'completed' THEN
    UPDATE shifde.run_steps SET status = 'cancelled', finished_at = now()
     WHERE run_id = p_run AND tenant_id = r.tenant_id AND status = 'running';
  END IF;

  -- 4. say it on the run, once: decisions are open and this pass opened some or the run was
  --    never told
  IF r.status = 'completed' THEN
    RETURN 0;
  END IF;
  SELECT count(*) INTO v_open FROM shifde.approvals
   WHERE run_id = p_run AND tenant_id = r.tenant_id
     AND reason = 'indeterminate_side_effect' AND status = 'pending';
  IF v_open = 0 THEN
    RETURN 0;
  END IF;
  IF v_new = 0 AND EXISTS (SELECT 1 FROM shifde.run_events
                            WHERE run_id = p_run AND tenant_id = r.tenant_id
                              AND type = 'outcome_unknown') THEN
    RETURN 0;
  END IF;
  SELECT COALESCE(details->>'previous_message', details->>'previous_error_class')
    INTO v_cause FROM shifde.run_events
   WHERE run_id = p_run AND tenant_id = r.tenant_id AND type = 'outcome_unknown'
   ORDER BY seq LIMIT 1;
  v_cause := COALESCE(v_cause, r.error_message, r.status);
  UPDATE shifde.runs
     SET error_class = 'indeterminate_side_effect',
         error_message = shifde.unknown_outcome_text(p_run, v_cause),
         event_seq = event_seq + 1
   WHERE id = p_run AND tenant_id = r.tenant_id RETURNING event_seq INTO v_seq;
  INSERT INTO shifde.run_events (id, tenant_id, run_id, seq, at, type, from_status, to_status,
         details)
  VALUES (gen_random_uuid(), r.tenant_id, p_run, v_seq, now(), 'outcome_unknown', r.status,
          r.status, jsonb_build_object('previous_error_class', r.error_class,
                                       'previous_message', r.error_message,
                                       'open_decisions', v_open, 'newly_opened', v_new));
  RETURN v_open;
END $$"""

_UNKNOWN = """IF shifde.settle_ended_run(r.rid) > 0 THEN
            tenant_id := r.tid; run_id := r.rid; reason := 'unknown_outcome'; RETURN NEXT;
          END IF;"""

REAPER = f"""CREATE FUNCTION shifde.shifde_reap_expired_leases()
RETURNS TABLE (tenant_id uuid, run_id uuid, reason text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS
$$
#variable_conflict use_column
DECLARE
  r record;
  v_seq int;
  v_sv int;
  v_status text;
  v_lease uuid;
BEGIN
{NO_CLIENT_CONTEXT}
  -- 1. expired job leases. The run is touched ONLY when its lease belongs to the expired job
  --    (runs.lease_job_id); a stale job of a run that moved on is just closed.
  FOR r IN SELECT j.id AS jid, j.tenant_id AS tid, j.run_id AS rid, j.attempts, j.max_attempts
             FROM shifde.jobs j
            WHERE j.status = 'leased' AND j.lease_expires_at < now()
              FOR UPDATE SKIP LOCKED
  LOOP
    v_status := NULL; v_lease := NULL;
    SELECT x.status, x.lease_job_id INTO v_status, v_lease
      FROM shifde.runs x WHERE x.id = r.rid AND x.tenant_id = r.tid;
    IF v_status = 'running' AND v_lease = r.jid THEN
      IF r.attempts < r.max_attempts THEN
        UPDATE shifde.jobs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
               run_after = now() + (LEAST(30, 1 << LEAST(r.attempts, 5)) * interval '1 second'),
               last_error = 'lease expired'
         WHERE id = r.jid AND tenant_id = r.tid;
        UPDATE shifde.runs SET status = 'pending', state_version = state_version + 1,
               lease_token = NULL, lease_job_id = NULL, event_seq = event_seq + 1
         WHERE id = r.rid AND tenant_id = r.tid AND status = 'running' AND lease_job_id = r.jid
         RETURNING event_seq INTO v_seq;
        IF FOUND THEN
          {_event("r.tid", "r.rid", "lease_lost", "'running'", "'pending'", "'{}'")}
          tenant_id := r.tid; run_id := r.rid; reason := 'lease_lost'; RETURN NEXT;
        END IF;
      ELSE
        UPDATE shifde.jobs SET status = 'dead', finished_at = now(),
               last_error = 'lease expired; attempts exhausted'
         WHERE id = r.jid AND tenant_id = r.tid;
        UPDATE shifde.runs SET status = 'failed', state_version = state_version + 1,
               lease_token = NULL, lease_job_id = NULL, finished_at = now(),
               error_class = 'internal', error_message = 'the worker was lost repeatedly',
               event_seq = event_seq + 1
         WHERE id = r.rid AND tenant_id = r.tid AND status = 'running' AND lease_job_id = r.jid
         RETURNING event_seq INTO v_seq;
        IF FOUND THEN
          {_event("r.tid", "r.rid", "state_change", "'running'", "'failed'", '\'{"error_class":"internal"}\'')}
          tenant_id := r.tid; run_id := r.rid; reason := 'worker_lost'; RETURN NEXT;
          {_UNKNOWN}
        END IF;
      END IF;
    ELSIF v_status = 'pending' AND NOT EXISTS (
            SELECT 1 FROM shifde.jobs o WHERE o.run_id = r.rid AND o.tenant_id = r.tid
               AND o.id <> r.jid AND o.status IN ('queued', 'leased')) THEN
      -- the worker died after claiming the job but before it claimed the run
      IF r.attempts < r.max_attempts THEN
        UPDATE shifde.jobs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
               run_after = now() + (LEAST(30, 1 << LEAST(r.attempts, 5)) * interval '1 second'),
               last_error = 'lease expired before the run started'
         WHERE id = r.jid AND tenant_id = r.tid;
      ELSE
        UPDATE shifde.jobs SET status = 'dead', finished_at = now(),
               last_error = 'lease expired; attempts exhausted'
         WHERE id = r.jid AND tenant_id = r.tid;
        UPDATE shifde.runs SET status = 'failed', state_version = state_version + 1,
               finished_at = now(), error_class = 'internal',
               error_message = 'the worker was lost repeatedly', event_seq = event_seq + 1
         WHERE id = r.rid AND tenant_id = r.tid AND status = 'pending'
         RETURNING event_seq INTO v_seq;
        IF FOUND THEN
          {_event("r.tid", "r.rid", "state_change", "'pending'", "'failed'", '\'{"error_class":"internal"}\'')}
          tenant_id := r.tid; run_id := r.rid; reason := 'worker_lost'; RETURN NEXT;
          {_UNKNOWN}
        END IF;
      END IF;
    ELSE
      UPDATE shifde.jobs SET status = 'done', finished_at = now(), last_error = 'stale lease'
       WHERE id = r.jid AND tenant_id = r.tid;
    END IF;
  END LOOP;

  -- 2. pending / waiting runs past their deadline or with an expired approval (an unknown-outcome
  --    approval never expires: a human decides it)
  FOR r IN SELECT x.id AS rid, x.tenant_id AS tid, x.status AS st,
                  EXISTS (SELECT 1 FROM shifde.approvals a WHERE a.run_id = x.id
                           AND a.tenant_id = x.tenant_id
                           AND a.status = 'pending' AND a.expires_at < now()
                           AND a.reason <> 'indeterminate_side_effect') AS approval_expired
             FROM shifde.runs x
            WHERE x.status IN ('pending', 'waiting_for_approval')
              AND (x.deadline_at < now()
                   OR (x.status = 'pending' AND x.queue_deadline_at IS NOT NULL
                       AND x.queue_deadline_at < now())
                   OR EXISTS (SELECT 1 FROM shifde.approvals a WHERE a.run_id = x.id
                               AND a.tenant_id = x.tenant_id
                               AND a.status = 'pending' AND a.expires_at < now()
                               AND a.reason <> 'indeterminate_side_effect'))
              FOR UPDATE SKIP LOCKED
  LOOP
    UPDATE shifde.runs SET status = 'timed_out', state_version = state_version + 1,
           lease_token = NULL, lease_job_id = NULL, finished_at = now(), error_class = 'timeout',
           error_message = CASE WHEN r.approval_expired THEN 'the approval expired'
                                ELSE 'the run deadline passed' END,
           event_seq = event_seq + 1
     WHERE id = r.rid AND tenant_id = r.tid RETURNING event_seq INTO v_seq;
    {_event("r.tid", "r.rid", "state_change", "r.st", "'timed_out'", '\'{"error_class":"timeout"}\'')}
    UPDATE shifde.approvals SET status = 'expired'
     WHERE run_id = r.rid AND tenant_id = r.tid AND status = 'pending'
       AND reason <> 'indeterminate_side_effect';
    UPDATE shifde.jobs SET status = 'done', finished_at = now()
     WHERE run_id = r.rid AND tenant_id = r.tid AND status = 'queued';
    tenant_id := r.tid; run_id := r.rid;
    reason := CASE WHEN r.approval_expired THEN 'approval_expired' ELSE 'timed_out' END;
    RETURN NEXT;
    {_UNKNOWN}
  END LOOP;

  -- 3. cancellation requested while the lease was lost: finalize
  FOR r IN SELECT x.id AS rid, x.tenant_id AS tid FROM shifde.runs x
            WHERE x.status = 'pending' AND x.cancel_requested_at IS NOT NULL
              FOR UPDATE SKIP LOCKED
  LOOP
    UPDATE shifde.runs SET status = 'cancelled', state_version = state_version + 1,
           lease_token = NULL, lease_job_id = NULL, finished_at = now(),
           error_class = 'cancelled', event_seq = event_seq + 1
     WHERE id = r.rid AND tenant_id = r.tid RETURNING event_seq INTO v_seq;
    {_event("r.tid", "r.rid", "state_change", "'pending'", "'cancelled'", "'{}'")}
    UPDATE shifde.jobs SET status = 'done', finished_at = now()
     WHERE run_id = r.rid AND tenant_id = r.tid AND status = 'queued';
    tenant_id := r.tid; run_id := r.rid; reason := 'cancelled'; RETURN NEXT;
    {_UNKNOWN}
  END LOOP;

  -- 4. parents waiting for child runs whose children are all finished (safety net)
  FOR r IN SELECT p.id AS rid, p.tenant_id AS tid FROM shifde.runs p
            WHERE p.status = 'waiting_for_approval' AND p.waiting_reason = 'child_runs'
              AND NOT EXISTS (SELECT 1 FROM shifde.runs c WHERE c.parent_run_id = p.id
                               AND c.tenant_id = p.tenant_id AND c.status NOT IN {TERMINAL})
              FOR UPDATE SKIP LOCKED
  LOOP
    UPDATE shifde.runs SET status = 'pending', state_version = state_version + 1,
           waiting_reason = NULL, event_seq = event_seq + 1
     WHERE id = r.rid AND tenant_id = r.tid RETURNING event_seq, state_version INTO v_seq, v_sv;
    {_event("r.tid", "r.rid", "state_change", "'waiting_for_approval'", "'pending'", "'{\"child_runs\":true}'")}
    INSERT INTO shifde.jobs (id, tenant_id, kind, run_id, dedupe_key)
    VALUES (gen_random_uuid(), r.tid, 'run.advance', r.rid,
            'run:' || r.rid::text || ':' || v_sv::text)
    ON CONFLICT DO NOTHING;
    tenant_id := r.tid; run_id := r.rid; reason := 'children_finished'; RETURN NEXT;
  END LOOP;
END $$"""

OPS_STATS = f"""CREATE FUNCTION shifde.shifde_ops_stats(p_window_seconds int)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS
$$
DECLARE
  v_since timestamptz := now() - make_interval(secs => GREATEST(p_window_seconds, 1));
  v_queue jsonb;
  v_runs jsonb;
  v_models jsonb;
  v_denials int;
BEGIN
{NO_CLIENT_CONTEXT}
  SELECT jsonb_build_object(
    'queued', count(*) FILTER (WHERE status = 'queued'),
    'leased', count(*) FILTER (WHERE status = 'leased'),
    'dead', count(*) FILTER (WHERE status = 'dead' AND finished_at >= v_since),
    'oldest_queued_age_seconds',
      COALESCE(EXTRACT(EPOCH FROM now() - min(run_after)
        FILTER (WHERE status = 'queued' AND run_after <= now())), 0)
  ) INTO v_queue FROM shifde.jobs;
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'kind', kind, 'status', status, 'error_class', COALESCE(error_class, ''), 'count', n)), '[]')
    INTO v_runs
    FROM (SELECT kind, status, error_class, count(*) AS n FROM shifde.runs
          WHERE finished_at >= v_since GROUP BY kind, status, error_class) r;
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'provider', COALESCE(provider_key, ''), 'calls', calls, 'failed', failed,
    'input_tokens', itok, 'output_tokens', otok, 'duration_ms_sum', dur)), '[]')
    INTO v_models
    FROM (SELECT provider_key, count(*) AS calls,
                 count(*) FILTER (WHERE status = 'failed') AS failed,
                 COALESCE(sum(input_tokens), 0) AS itok, COALESCE(sum(output_tokens), 0) AS otok,
                 COALESCE(sum(duration_ms), 0) AS dur
          FROM shifde.usage_events
          WHERE event_type IN ('model_call', 'prompt_test') AND occurred_at >= v_since
            AND corrects_event_id IS NULL
          GROUP BY provider_key) m;
  SELECT count(*) INTO v_denials FROM shifde.policy_decisions
    WHERE at >= v_since AND decision = 'deny' AND 'budget_denied' = ANY (reason_codes);
  RETURN jsonb_build_object(
    'window_seconds', p_window_seconds, 'queue', v_queue, 'runs', v_runs, 'models', v_models,
    'budget_denials', v_denials);
END
$$"""

DEFINERS = (
    "shifde.shifde_claim_jobs(text, int, int, int)",
    "shifde.shifde_reap_expired_leases()",
    "shifde.shifde_ops_stats(int)",
)
INVOKERS = (
    "shifde.land_verdicts(uuid)",
    "shifde.unknown_outcome_text(uuid, text)",
    "shifde.settle_ended_run(uuid)",
)


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ transitions (global)
        """CREATE TABLE shifde.run_state_transitions (
            from_status text NOT NULL, to_status text NOT NULL,
            PRIMARY KEY (from_status, to_status))""",
        *[
            f"INSERT INTO shifde.run_state_transitions VALUES ('{a}', '{b}')"
            for a, b in TRANSITIONS
        ],
        "ALTER TABLE shifde.run_state_transitions ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.run_state_transitions FORCE ROW LEVEL SECURITY",
        "CREATE POLICY read_all ON shifde.run_state_transitions FOR SELECT USING (true)",
        "GRANT SELECT ON shifde.run_state_transitions TO shifde_app",
        # ------------------------------------------------------------------ workers (global)
        """CREATE TABLE shifde.workers (
            id text PRIMARY KEY,
            hostname text NOT NULL,
            pid int NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            heartbeat_at timestamptz NOT NULL DEFAULT now(),
            version text NOT NULL,
            concurrency int NOT NULL CHECK (concurrency > 0),
            status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'stopped')),
            last_reap_at timestamptz
        )""",
        "ALTER TABLE shifde.workers ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.workers FORCE ROW LEVEL SECURITY",
        "CREATE POLICY worker_rows ON shifde.workers USING (true) WITH CHECK (true)",
        "GRANT SELECT, INSERT ON shifde.workers TO shifde_app",
        "GRANT UPDATE (hostname, pid, started_at, heartbeat_at, version, concurrency, status, "
        "last_reap_at) ON shifde.workers TO shifde_app",
        # ------------------------------------------------------------------ runs
        f"""CREATE TABLE shifde.runs (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            workspace_id uuid NOT NULL,
            project_id uuid NOT NULL,
            kind text NOT NULL CHECK (kind IN
                ('agent', 'orchestration', 'pipeline', 'ontology_action')),
            status text NOT NULL DEFAULT 'pending' CHECK (status IN {STATUSES}),
            state_version int NOT NULL DEFAULT 1,
            event_seq int NOT NULL DEFAULT 0,
            lease_token uuid,
            lease_job_id uuid,
            initiated_by_principal_id uuid NOT NULL,
            principal_type text NOT NULL CHECK (principal_type IN ('user', 'service_account')),
            api_key_id uuid,
            agent_id uuid,
            agent_version_id uuid,
            prompt_version_id uuid,
            orchestration_id uuid,
            orchestration_version_id uuid,
            parent_run_id uuid,
            root_run_id uuid NOT NULL,
            node_id text,
            depth int NOT NULL DEFAULT 0 CHECK (depth >= 0),
            is_test boolean NOT NULL DEFAULT false,
            input_ref uuid,
            output_ref uuid,
            error_class text,
            error_message text,
            waiting_reason text,
            idempotency_key text,
            request_hash text,
            trace_id text NOT NULL,
            correlation_id text NOT NULL,
            queued_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            deadline_at timestamptz NOT NULL,
            queue_deadline_at timestamptz,
            cancel_requested_at timestamptz,
            cancel_requested_by uuid,
            attempt_count int NOT NULL DEFAULT 0,
            input_tokens bigint NOT NULL DEFAULT 0,
            output_tokens bigint NOT NULL DEFAULT 0,
            cached_tokens bigint NOT NULL DEFAULT 0,
            model_calls int NOT NULL DEFAULT 0,
            tool_calls int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            pipeline_id uuid,
            pipeline_version_id uuid,
            ontology_id uuid,
            ontology_version_id uuid,
            action_key text,
            CONSTRAINT uq_runs__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_runs__target CHECK
                ((kind = 'agent' AND agent_version_id IS NOT NULL)
                 OR (kind = 'orchestration' AND orchestration_version_id IS NOT NULL)
                 OR (kind = 'pipeline' AND pipeline_version_id IS NOT NULL
                     AND pipeline_id IS NOT NULL)
                 OR (kind = 'ontology_action' AND ontology_version_id IS NOT NULL
                     AND ontology_id IS NOT NULL AND action_key IS NOT NULL)),
            CONSTRAINT fk_runs__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id),
            CONSTRAINT fk_runs__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id),
            CONSTRAINT fk_runs__agent_version FOREIGN KEY (tenant_id, agent_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id),
            CONSTRAINT fk_runs__orch_version FOREIGN KEY (tenant_id, orchestration_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id),
            CONSTRAINT fk_runs__pipeline_version FOREIGN KEY (tenant_id, pipeline_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id),
            CONSTRAINT fk_runs__pipeline_of_version
                FOREIGN KEY (tenant_id, pipeline_version_id, pipeline_id)
                REFERENCES shifde.artifact_versions (tenant_id, id, artifact_id),
            CONSTRAINT fk_runs__ontology_of_version
                FOREIGN KEY (tenant_id, ontology_version_id, ontology_id)
                REFERENCES shifde.artifact_versions (tenant_id, id, artifact_id),
            CONSTRAINT fk_runs__parent FOREIGN KEY (tenant_id, parent_run_id)
                REFERENCES shifde.runs (tenant_id, id),
            CONSTRAINT fk_runs__root FOREIGN KEY (tenant_id, root_run_id)
                REFERENCES shifde.runs (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_runs__idempotency ON shifde.runs "
        "(tenant_id, initiated_by_principal_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
        "CREATE INDEX ix_runs__tenant_id ON shifde.runs (tenant_id, id DESC)",
        "CREATE INDEX ix_runs__parent ON shifde.runs (tenant_id, parent_run_id) "
        "WHERE parent_run_id IS NOT NULL",
        "CREATE INDEX ix_runs__root ON shifde.runs (tenant_id, root_run_id)",
        "CREATE INDEX ix_runs__open ON shifde.runs (status, deadline_at) "
        "WHERE status IN ('pending', 'running', 'waiting_for_approval')",
        "CREATE INDEX ix_runs__pipeline ON shifde.runs (tenant_id, pipeline_id, id DESC) "
        "WHERE pipeline_id IS NOT NULL",
        "CREATE INDEX ix_runs__ontology ON shifde.runs (tenant_id, ontology_id, id DESC) "
        "WHERE ontology_id IS NOT NULL",
        RUNS_GUARD,
        "REVOKE ALL ON FUNCTION shifde.runs_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_runs_guard BEFORE UPDATE ON shifde.runs "
        "FOR EACH ROW EXECUTE FUNCTION shifde.runs_guard()",
        *_tenant_table("runs"),
        "GRANT SELECT, INSERT ON shifde.runs TO shifde_app",
        "GRANT UPDATE (status, state_version, event_seq, lease_token, lease_job_id, input_ref, "
        "output_ref, error_class, error_message, waiting_reason, started_at, finished_at, "
        "cancel_requested_at, cancel_requested_by, attempt_count, input_tokens, output_tokens, "
        "cached_tokens, model_calls, tool_calls) ON shifde.runs TO shifde_app",
        # ------------------------------------------------------------------ run events
        """CREATE TABLE shifde.run_events (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            run_id uuid NOT NULL,
            seq int NOT NULL,
            at timestamptz NOT NULL DEFAULT now(),
            type text NOT NULL,
            from_status text,
            to_status text,
            step_id uuid,
            details jsonb NOT NULL DEFAULT '{}',
            CONSTRAINT uq_run_events__seq UNIQUE (tenant_id, run_id, seq),
            CONSTRAINT fk_run_events__run FOREIGN KEY (tenant_id, run_id)
                REFERENCES shifde.runs (tenant_id, id)
        )""",
        "CREATE TRIGGER trg_run_events_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.run_events FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
        *_tenant_table("run_events"),
        "GRANT SELECT, INSERT ON shifde.run_events TO shifde_app",
        # ------------------------------------------------------------------ run artifacts
        """CREATE TABLE shifde.run_artifacts (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            run_id uuid NOT NULL,
            step_id uuid,
            kind text NOT NULL CHECK (kind IN ('input', 'output', 'intermediate', 'transcript')),
            content_type text NOT NULL DEFAULT 'application/json',
            size_bytes int NOT NULL,
            content jsonb NOT NULL,
            sha256 text NOT NULL,
            classification text NOT NULL DEFAULT 'internal',
            expires_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_run_artifacts__run FOREIGN KEY (tenant_id, run_id)
                REFERENCES shifde.runs (tenant_id, id)
        )""",
        "CREATE INDEX ix_run_artifacts__run ON shifde.run_artifacts (tenant_id, run_id)",
        "CREATE TRIGGER trg_run_artifacts_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.run_artifacts FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
        *_tenant_table("run_artifacts"),
        "GRANT SELECT, INSERT ON shifde.run_artifacts TO shifde_app",
        # ------------------------------------------------------------------ tool invocations
        """CREATE TABLE shifde.tool_invocations (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            run_id uuid NOT NULL,
            call_ref text NOT NULL,
            epoch int NOT NULL DEFAULT 0,
            tool_version_id uuid NOT NULL,
            side_effect_class text NOT NULL,
            idempotency_key text NOT NULL,
            status text NOT NULL CHECK (status IN
                ('started', 'succeeded', 'failed', 'indeterminate',
                 'resolved_succeeded', 'resolved_retry', 'resolved_failed')),
            attempts int NOT NULL DEFAULT 1,
            request_hash text NOT NULL,
            response_ref uuid,
            error_class text,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            arguments jsonb,
            CONSTRAINT uq_tool_invocations__key UNIQUE (tenant_id, idempotency_key),
            CONSTRAINT uq_tool_invocations__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_tool_invocations__run FOREIGN KEY (tenant_id, run_id)
                REFERENCES shifde.runs (tenant_id, id),
            CONSTRAINT fk_tool_invocations__version FOREIGN KEY (tenant_id, tool_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        "CREATE INDEX ix_tool_invocations__run ON shifde.tool_invocations (tenant_id, run_id)",
        *_tenant_table("tool_invocations"),
        "GRANT SELECT, INSERT ON shifde.tool_invocations TO shifde_app",
        "GRANT UPDATE (status, attempts, response_ref, error_class, started_at, finished_at) "
        "ON shifde.tool_invocations TO shifde_app",
        # ------------------------------------------------------------------ steps
        """CREATE TABLE shifde.run_steps (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            run_id uuid NOT NULL,
            seq int NOT NULL,
            attempt int NOT NULL DEFAULT 1,
            kind text NOT NULL CHECK (kind IN
                ('model_call', 'tool_call', 'approval', 'sub_agent', 'orchestration_node',
                 'pipeline_node')),
            name text NOT NULL,
            node_id text,
            parent_seq int,
            call_index int,
            status text NOT NULL DEFAULT 'running' CHECK (status IN
                ('pending', 'running', 'succeeded', 'failed', 'skipped', 'cancelled',
                 'indeterminate')),
            model_id uuid,
            tool_version_id uuid,
            tool_invocation_id uuid,
            child_run_id uuid,
            idempotency_key text,
            error_class text,
            error_message text,
            input_ref uuid,
            output_ref uuid,
            usage jsonb NOT NULL DEFAULT '{}',
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CONSTRAINT uq_run_steps__attempt UNIQUE (tenant_id, run_id, seq, attempt),
            CONSTRAINT fk_run_steps__run FOREIGN KEY (tenant_id, run_id)
                REFERENCES shifde.runs (tenant_id, id),
            CONSTRAINT fk_run_steps__child_run FOREIGN KEY (tenant_id, child_run_id)
                REFERENCES shifde.runs (tenant_id, id),
            CONSTRAINT fk_run_steps__tool_invocation FOREIGN KEY (tenant_id, tool_invocation_id)
                REFERENCES shifde.tool_invocations (tenant_id, id),
            CONSTRAINT fk_run_steps__tool_version FOREIGN KEY (tenant_id, tool_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        *_tenant_table("run_steps"),
        "GRANT SELECT, INSERT ON shifde.run_steps TO shifde_app",
        "GRANT UPDATE (status, tool_invocation_id, child_run_id, error_class, error_message, "
        "input_ref, output_ref, usage, finished_at) ON shifde.run_steps TO shifde_app",
        # ------------------------------------------------------------------ approvals
        """CREATE TABLE shifde.approvals (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            run_id uuid NOT NULL,
            workspace_id uuid NOT NULL,
            project_id uuid NOT NULL,
            step_seq int,
            call_ref text NOT NULL,
            epoch int NOT NULL DEFAULT 0,
            reason text NOT NULL CHECK (reason IN
                ('tool_side_effect', 'approval_rule', 'orchestration_node',
                 'indeterminate_side_effect', 'pipeline_node')),
            tool_version_id uuid,
            tool_key text,
            tool_version_no int,
            side_effect_class text,
            arguments jsonb NOT NULL DEFAULT '{}',
            context_summary jsonb NOT NULL DEFAULT '{}',
            required_permission text NOT NULL DEFAULT 'approval:decide',
            requested_by uuid NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            status text NOT NULL DEFAULT 'pending' CHECK (status IN
                ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
            decision text CHECK (decision IN
                ('approve', 'reject', 'mark_succeeded', 'retry', 'fail')),
            decided_by uuid,
            decided_at timestamptz,
            decision_note text,
            CONSTRAINT ck_approvals__decided CHECK
                ((status IN ('approved', 'rejected')) = (decided_by IS NOT NULL)),
            CONSTRAINT fk_approvals__run FOREIGN KEY (tenant_id, run_id)
                REFERENCES shifde.runs (tenant_id, id),
            CONSTRAINT fk_approvals__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id),
            CONSTRAINT fk_approvals__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id),
            CONSTRAINT fk_approvals__tool_version FOREIGN KEY (tenant_id, tool_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_approvals__call ON shifde.approvals "
        "(tenant_id, run_id, call_ref, reason, epoch)",
        "CREATE INDEX ix_approvals__status ON shifde.approvals (tenant_id, status, id)",
        APPROVALS_GUARD,
        "REVOKE ALL ON FUNCTION shifde.approvals_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_approvals_guard BEFORE UPDATE ON shifde.approvals "
        "FOR EACH ROW EXECUTE FUNCTION shifde.approvals_guard()",
        *_tenant_table("approvals"),
        "GRANT SELECT, INSERT ON shifde.approvals TO shifde_app",
        "GRANT UPDATE (status, decision, decided_by, decided_at, decision_note) "
        "ON shifde.approvals TO shifde_app",
        # ------------------------------------------------------------------ jobs
        """CREATE TABLE shifde.jobs (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            kind text NOT NULL,
            run_id uuid,
            payload jsonb NOT NULL DEFAULT '{}',
            priority smallint NOT NULL DEFAULT 100,
            status text NOT NULL DEFAULT 'queued' CHECK (status IN
                ('queued', 'leased', 'done', 'dead')),
            run_after timestamptz NOT NULL DEFAULT now(),
            attempts int NOT NULL DEFAULT 0,
            max_attempts int NOT NULL DEFAULT 5,
            lease_owner text,
            lease_expires_at timestamptz,
            heartbeat_at timestamptz,
            dedupe_key text NOT NULL,
            traceparent text,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CONSTRAINT fk_jobs__run FOREIGN KEY (tenant_id, run_id)
                REFERENCES shifde.runs (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_jobs__dedupe ON shifde.jobs (tenant_id, kind, dedupe_key) "
        "WHERE status IN ('queued', 'leased')",
        "CREATE INDEX ix_jobs__claim ON shifde.jobs (priority, run_after) WHERE status = 'queued'",
        "CREATE INDEX ix_jobs__lease ON shifde.jobs (lease_expires_at) WHERE status = 'leased'",
        *_tenant_table("jobs"),
        "GRANT SELECT, INSERT ON shifde.jobs TO shifde_app",
        "GRANT UPDATE (status, lease_owner, lease_expires_at, heartbeat_at, last_error, "
        "finished_at, run_after, attempts) ON shifde.jobs TO shifde_app",
        # ------------------------------------------------------------------ deferred foreign keys
        "ALTER TABLE shifde.budget_reservations ADD CONSTRAINT fk_budget_reservations__run "
        "FOREIGN KEY (tenant_id, run_id) REFERENCES shifde.runs (tenant_id, id)",
        "ALTER TABLE shifde.ontology_query_lineage ADD CONSTRAINT fk_ontology_lineage__run "
        "FOREIGN KEY (tenant_id, run_id) REFERENCES shifde.runs (tenant_id, id)",
        # ------------------------------------------------------------------ functions
        CLAIM,
        LAND,
        TEXT,
        SETTLE,
        REAPER,
        OPS_STATS,
        *[f"REVOKE ALL ON FUNCTION {f} FROM PUBLIC" for f in (*DEFINERS, *INVOKERS)],
        *[f"GRANT EXECUTE ON FUNCTION {f} TO shifde_app" for f in (*DEFINERS, *INVOKERS)],
    )


def downgrade() -> None:
    _run(
        *[f"DROP FUNCTION IF EXISTS {f}" for f in (*DEFINERS, *reversed(INVOKERS))],
        "ALTER TABLE shifde.ontology_query_lineage DROP CONSTRAINT IF EXISTS "
        "fk_ontology_lineage__run",
        "ALTER TABLE shifde.budget_reservations DROP CONSTRAINT IF EXISTS "
        "fk_budget_reservations__run",
        *[f"DROP TABLE IF EXISTS shifde.{t} CASCADE" for t in DROP_ORDER],
        "DROP FUNCTION IF EXISTS shifde.approvals_guard()",
        "DROP FUNCTION IF EXISTS shifde.runs_guard()",
    )
