'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useEffect, useState } from 'react';
import type { Run } from '@/lib/api/operate-types';
import { ApiError } from '@/lib/api/errors';
import { useRun, useRunApprovals, useRunResult } from '@/lib/operate-hooks';
import { orgHref } from '@/lib/scope';
import { ActionError, PageHeader, QueryGate, formatDate } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { CancelRunButton } from './CancelRunButton';
import { CopyButton, FailurePanel, ResultView, RunStatusChip, UsageSummary } from './parts';
import { RunNodesTab } from '../pipelines/RunNodesTab';
import { ApprovalDetail } from './ApprovalsView';
import { Timeline } from './Timeline';
import { principalLabel, UNKNOWN_OUTCOME_LABEL, waitingText } from '@/lib/run-utils';

type Tab = 'overview' | 'timeline' | 'nodes';

export function IdentifiersPanel({ run }: { run: Run }) {
  const rows: [string, string | null | undefined, boolean][] = [
    ['Tenant', run.tenant_id, true],
    ['Workspace', `${run.workspace.slug} (${run.workspace.id})`, false],
    ['Project', `${run.project.slug} (${run.project.id})`, false],
    ['Principal', `${principalLabel(run.initiated_by)} (${run.initiated_by.type})`, false],
    ['Run', run.id, true],
    ['Agent version', run.agent_version_id, true],
    ['Prompt version', run.prompt_version_id, true],
    ['Orchestration version', run.orchestration_version_id, true],
    ['Pipeline', run.pipeline_id, true],
    ['Pipeline version', run.pipeline_version_id, true],
    ['Parent run', run.parent_run_id, true],
    ['Trace id', run.trace_id, true],
    ['Correlation id', run.correlation_id, true],
    ['Idempotency key', run.idempotency_key, true],
  ];
  return (
    <section aria-label="Identifiers" className="card">
      <h3 className="text-sm font-semibold">Identifiers</h3>
      <dl className="mt-2 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
        {rows.map(([label, value, copy]) => (
          <div key={label} className="contents">
            <dt className="text-slate-600">{label}</dt>
            <dd className="break-all">
              {value ? (
                <>
                  <code className="font-mono text-xs">{value}</code>
                  {copy && <CopyButton value={value} label={label} />}
                </>
              ) : (
                '-'
              )}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** Open unknown-write decision(s) of this run, with the decision form (R1.4). */
function UnknownOutcomePanel({ org, run }: { org: string; run: Run }) {
  // The API decides who may see and answer a decision: useCan checks tenant scope only, and a
  // workspace or project approver holds no tenant-level grant. Only a real 403 means no permission.
  const query = useRunApprovals(org, run.id, true);
  const forbidden = query.error instanceof ApiError && query.error.status === 403;
  useEffect(() => {
    // The panel mounts after the run loads, so the browser has already tried the #decision anchor.
    if (window.location.hash === '#decision')
      document.getElementById('decision')?.scrollIntoView?.();
  }, []);
  const pending = (query.data?.items ?? []).filter((a) => a.reason === 'indeterminate_side_effect');
  return (
    <section
      id="decision"
      aria-label="Write outcome unknown"
      className="card space-y-3 border-amber-300 bg-amber-50"
    >
      <h3 className="text-sm font-semibold text-amber-900">{UNKNOWN_OUTCOME_LABEL}</h3>
      <p className="text-sm">
        A write in this run, or in a run it started, may have been delivered, and the platform
        cannot confirm whether it happened. Check the target system, then record whether it did.
      </p>
      {!forbidden && (
        <Link className="text-sm text-blue-800 underline" href={`${orgHref(org)}/approvals`}>
          Open in the approval queue
        </Link>
      )}
      {query.isPending ? (
        <p role="status" className="text-sm text-slate-600">
          Loading the decision...
        </p>
      ) : forbidden ? (
        <p className="text-sm text-slate-700">
          A decision is open for this write, but the server says you do not have permission to see
          it. Ask someone who can view approvals.
        </p>
      ) : query.isError ? (
        <ActionError error={query.error} title="Could not load the decision" />
      ) : pending.length === 0 ? (
        <p className="text-sm text-slate-700">
          A decision is open for this write, but none is visible to you. It may belong to a scope
          you cannot see.
        </p>
      ) : (
        pending.map((a) => (
          <div key={a.id} className="space-y-2">
            {a.run_id !== run.id && (
              <p className="text-sm">
                This decision belongs to a child run:{' '}
                <Link className="text-blue-800 underline" href={`${orgHref(org)}/runs/${a.run_id}`}>
                  {a.run_id}
                </Link>
              </p>
            )}
            <ApprovalDetail org={org} id={a.id} />
          </div>
        ))
      )}
    </section>
  );
}

function Overview({ org, run }: { org: string; run: Run }) {
  const result = useRunResult(org, run.id, run.status === 'completed');
  return (
    <div className="space-y-4">
      {run.status === 'waiting_for_approval' && (
        <div role="status" className="card border-amber-300 bg-amber-50 text-sm">
          <p className="font-medium text-amber-900">Waiting for approval</p>
          <p>{waitingText(run, 'An approver must decide before this run continues.')}</p>
          <Link className="text-blue-800 underline" href={`${orgHref(org)}/approvals`}>
            Open the approval queue
          </Link>
        </div>
      )}
      {run.outcome_unknown && <UnknownOutcomePanel org={org} run={run} />}
      <FailurePanel run={run} />
      <dl className="card grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
        <dt className="text-slate-600">Kind</dt>
        <dd>{run.kind}</dd>
        <dt className="text-slate-600">Created</dt>
        <dd>{formatDate(run.created_at)}</dd>
        <dt className="text-slate-600">Started</dt>
        <dd>{formatDate(run.started_at)}</dd>
        <dt className="text-slate-600">Finished</dt>
        <dd>{formatDate(run.finished_at)}</dd>
        <dt className="text-slate-600">Deadline</dt>
        <dd>{formatDate(run.deadline_at)}</dd>
        <dt className="text-slate-600">Attempts</dt>
        <dd>{run.attempt_count}</dd>
      </dl>
      <UsageSummary run={run} />
      {run.status === 'completed' && (
        <QueryGate query={result} noun="result">
          {(r) => <ResultView result={r} />}
        </QueryGate>
      )}
      <IdentifiersPanel run={run} />
    </div>
  );
}

export function RunDetailView({ id }: { id: string }) {
  const org = useOrgParam();
  const query = useRun(org, id);
  return (
    <div className="space-y-6">
      <p className="text-sm">
        <Link className="text-blue-800 underline" href={`${orgHref(org)}/runs`}>
          Runs
        </Link>
      </p>
      <QueryGate query={query} noun="run">
        {(run) => <Detail org={org} run={run} />}
      </QueryGate>
    </div>
  );
}

function Detail({ org, run }: { org: string; run: Run }) {
  const tabs: { id: Tab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'timeline', label: 'Execution timeline' },
    ...(run.kind === 'pipeline' ? [{ id: 'nodes' as const, label: 'Nodes' }] : []),
  ];
  // ?tab=overview|timeline|nodes selects the tab (node links point at ?tab=overview#decision).
  const wanted = useSearchParams()?.get('tab');
  const fromUrl: Tab = tabs.find((t) => t.id === wanted)?.id ?? 'overview';
  const [tab, setTab] = useState<Tab>(fromUrl);
  // A link to this same page with another ?tab= changes the URL, not this component: follow it
  // during render rather than in an effect.
  const [seenUrlTab, setSeenUrlTab] = useState<Tab>(fromUrl);
  if (seenUrlTab !== fromUrl) {
    setSeenUrlTab(fromUrl);
    setTab(fromUrl);
  }
  return (
    <>
      <PageHeader
        title={`Run ${run.id}`}
        actions={
          <div className="flex items-center gap-3">
            <RunStatusChip status={run.status} outcomeUnknown={run.outcome_unknown} />
            <CancelRunButton org={org} run={run} />
          </div>
        }
      />
      <div
        role="tablist"
        aria-label="Run sections"
        className="flex gap-1 border-b border-slate-200"
      >
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`}
            className={`px-3 py-2 text-sm font-medium ${
              tab === t.id ? 'border-b-2 border-blue-800 text-blue-900' : 'text-slate-600'
            }`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === 'overview' && <Overview org={org} run={run} />}
        {tab === 'timeline' && <Timeline org={org} run={run} />}
        {tab === 'nodes' && <RunNodesTab org={org} run={run} />}
      </div>
    </>
  );
}
