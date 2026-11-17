'use client';

import Link from 'next/link';
import { usePipelineRuns } from '@/lib/pipeline-hooks';
import { principalLabel } from '@/lib/run-utils';
import { orgHref } from '@/lib/scope';
import { Badge, EmptyState, formatDate } from '../admin/ui';
import { QueryError } from '../QueryError';
import { RunOutcomeNote, RunStatusChip } from '../operate/parts';

/** Pipeline History: runs of this pipeline (test runs labelled), each linking to the run detail. */
export function PipelineHistory({ org, pipelineId }: { org: string; pipelineId: string }) {
  const query = usePipelineRuns(org, pipelineId);
  if (query.isPending) return <p role="status">Loading runs...</p>;
  if (query.isError) return <QueryError error={query.error} />;
  const rows = query.data.pages.flatMap((p) => p.items);
  if (rows.length === 0) return <EmptyState>No runs yet.</EmptyState>;
  return (
    <section aria-label="Pipeline runs" className="card p-0">
      <table className="w-full">
        <caption className="sr-only">Pipeline runs</caption>
        <thead className="border-b border-slate-200">
          <tr>
            {['Run', 'Status', 'Kind', 'Started by', 'Created', 'Finished'].map((h) => (
              <th key={h} scope="col" className="th">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((r) => (
            <tr key={r.id}>
              <td className="td">
                <Link
                  className="font-mono text-xs text-blue-800 underline"
                  href={`${orgHref(org)}/runs/${r.id}`}
                >
                  {r.id}
                </Link>
              </td>
              <td className="td">
                <RunStatusChip status={r.status} outcomeUnknown={r.outcome_unknown} />
                <RunOutcomeNote run={r} />
              </td>
              <td className="td">
                {r.is_test ? <Badge tone="warn">Test run</Badge> : <Badge>Execution</Badge>}
              </td>
              <td className="td">{principalLabel(r.initiated_by)}</td>
              <td className="td">{formatDate(r.created_at)}</td>
              <td className="td">{formatDate(r.finished_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {query.hasNextPage && (
        <div className="p-3">
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
    </section>
  );
}
