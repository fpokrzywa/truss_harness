'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useState } from 'react';
import type { RunFilters } from '@/lib/api/operate-types';
import { useArtifacts } from '@/lib/build-hooks';
import { useRuns } from '@/lib/operate-hooks';
import { orgHref, projHref } from '@/lib/scope';
import { principalLabel, STATUS_LABEL } from '@/lib/run-utils';
import { EmptyState, formatDate, PageHeader, SelectField, TextField } from '../admin/ui';
import { QueryError } from '../QueryError';
import { useOrgParam } from '../security/useOrg';
import { RunOutcomeNote, RunStatusChip } from './parts';

const startOfDay = (d: string) => (d ? new Date(`${d}T00:00:00`).toISOString() : '');
const endOfDay = (d: string) => (d ? new Date(`${d}T23:59:59.999`).toISOString() : '');

/** Run History for the org, or a project when mounted under /w/[ws]/p/[proj]/runs. */
export function RunHistoryView() {
  const org = useOrgParam();
  const params = useParams<{ ws?: string; proj?: string }>();
  const ws = params.ws ? decodeURIComponent(params.ws) : undefined;
  const proj = params.proj ? decodeURIComponent(params.proj) : undefined;

  const [status, setStatus] = useState('');
  const [kind, setKind] = useState('');
  const [outcomeUnknown, setOutcomeUnknown] = useState('');
  const [agentId, setAgentId] = useState('');
  const [initiatedBy, setInitiatedBy] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');

  const agents = useArtifacts(org, 'agents', {});
  const filters: RunFilters = {
    status,
    kind,
    outcome_unknown: outcomeUnknown,
    agent_id: agentId,
    initiated_by: initiatedBy.trim(),
    from: startOfDay(from),
    to: endOfDay(to),
    workspace: ws,
    project: proj,
  };
  const query = useRuns(org, filters);
  const rows = query.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title={proj ? `Runs: ${proj}` : 'Runs'}
        description={
          proj ? `Runs in project ${proj}.` : 'Agent and orchestration runs you are allowed to see.'
        }
        actions={
          ws && proj ? (
            <Link className="btn btn-primary" href={`${projHref(org, ws, proj)}/run`}>
              Start a run
            </Link>
          ) : undefined
        }
      />
      <form
        aria-label="Run filters"
        className="grid gap-3 sm:grid-cols-3 lg:grid-cols-4"
        onSubmit={(e) => e.preventDefault()}
      >
        <SelectField
          label="Status"
          value={status}
          onChange={setStatus}
          options={[
            { value: '', label: 'Any status' },
            ...Object.entries(STATUS_LABEL).map(([value, label]) => ({ value, label })),
          ]}
        />
        <SelectField
          label="Kind"
          value={kind}
          onChange={setKind}
          options={[
            { value: '', label: 'Any kind' },
            { value: 'agent', label: 'Agent' },
            { value: 'orchestration', label: 'Orchestration' },
          ]}
        />
        <SelectField
          label="Write outcome"
          value={outcomeUnknown}
          onChange={setOutcomeUnknown}
          options={[
            { value: '', label: 'Any' },
            { value: 'true', label: 'Unknown only' },
          ]}
        />
        <SelectField
          label="Agent"
          value={agentId}
          onChange={setAgentId}
          options={[
            { value: '', label: 'Any agent' },
            ...(agents.data?.items ?? []).map((a) => ({ value: a.id, label: a.name })),
          ]}
        />
        <TextField label="Initiated by" value={initiatedBy} onChange={setInitiatedBy} />
        <TextField label="From date" type="date" value={from} onChange={setFrom} />
        <TextField label="To date" type="date" value={to} onChange={setTo} />
      </form>

      {query.isPending ? (
        <p role="status" className="text-sm text-slate-600">
          Loading runs...
        </p>
      ) : query.isError ? (
        <QueryError error={query.error} />
      ) : (
        <section aria-label="Runs" className="card p-0">
          {rows.length === 0 ? (
            <EmptyState>No runs match.</EmptyState>
          ) : (
            <table className="w-full">
              <caption className="sr-only">Runs</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {['Run', 'Status', 'Kind', 'Scope', 'Initiated by', 'Created'].map((h) => (
                    <th key={h} scope="col" className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td className="td font-mono text-xs">
                      <Link
                        className="text-blue-800 underline"
                        href={`${orgHref(org)}/runs/${r.id}`}
                      >
                        {r.id}
                      </Link>
                    </td>
                    <td className="td">
                      <RunStatusChip status={r.status} outcomeUnknown={r.outcome_unknown} />
                      <RunOutcomeNote run={r} />
                    </td>
                    <td className="td">{r.kind}</td>
                    <td className="td">
                      {r.workspace.slug} / {r.project.slug}
                    </td>
                    <td className="td">{principalLabel(r.initiated_by)}</td>
                    <td className="td">{formatDate(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {query.hasNextPage && (
            <div className="border-t border-slate-200 p-3">
              <button
                type="button"
                className="btn"
                disabled={query.isFetchingNextPage}
                onClick={() => void query.fetchNextPage()}
              >
                {query.isFetchingNextPage ? 'Loading...' : 'Load more'}
              </button>
            </div>
          )}
          {query.isFetchNextPageError && (
            <p role="alert" className="p-3 text-sm text-red-800">
              Could not load more runs. Try again.
            </p>
          )}
        </section>
      )}
    </div>
  );
}
