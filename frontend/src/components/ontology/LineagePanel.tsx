'use client';

import Link from 'next/link';
import { useState } from 'react';
import type { Artifact } from '@/lib/api/build-types';
import type { LineageRecord } from '@/lib/api/ontology-types';
import { useLineage } from '@/lib/ontology-hooks';
import { orgHref } from '@/lib/scope';
import { EmptyState, TextField, formatDate } from '../admin/ui';
import { QueryError } from '../QueryError';

function principalText(p: unknown): string {
  if (typeof p === 'string') return p;
  if (typeof p === 'object' && p !== null) {
    const r = p as Record<string, unknown>;
    const name = r.display ?? r.display_name ?? r.email ?? r.id;
    return typeof name === 'string' ? name : JSON.stringify(p);
  }
  return '';
}

/** Row counts may be a number, a map per object type/connection, or a list; shown as plain counts. */
function countsText(c: unknown): string {
  if (typeof c === 'number') return String(c);
  if (Array.isArray(c)) return c.map(String).join(', ');
  if (typeof c === 'object' && c !== null)
    return Object.entries(c)
      .map(([k, v]) => `${k}: ${String(v)}`)
      .join(', ');
  return '';
}

/** Lineage records: object types, connections, row counts, durations and query hashes (never values). */
export function LineagePanel({
  org,
  artifact,
  highlight,
}: {
  org: string;
  artifact: Artifact;
  highlight?: string;
}) {
  const [runId, setRunId] = useState('');
  const q = useLineage(org, artifact.id, runId.trim());
  const records: LineageRecord[] = q.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-700">
        One record per query. Records hold hashes of the compiled queries and row counts, never the
        data itself.
      </p>
      <div className="max-w-sm">
        <TextField label="Filter by run id" value={runId} onChange={setRunId} monospace />
      </div>
      {q.isPending ? (
        <p role="status" className="text-sm text-slate-600">
          Loading lineage...
        </p>
      ) : q.isError ? (
        <QueryError error={q.error} />
      ) : (
        <section aria-label="Lineage records" className="card overflow-x-auto p-0">
          {records.length === 0 ? (
            <EmptyState>No lineage records.</EmptyState>
          ) : (
            <table className="w-full text-sm">
              <caption className="sr-only">Lineage records</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {[
                    'When',
                    'Principal',
                    'Run',
                    'Object types',
                    'Connections',
                    'Row counts',
                    'Duration',
                    'Query hashes',
                  ].map((h) => (
                    <th key={h} scope="col" className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {records.map((r) => (
                  <tr key={r.id} className={r.id === highlight ? 'bg-amber-50' : undefined}>
                    <td className="td">
                      {formatDate(r.at)}
                      {r.id === highlight && <span className="ml-1 font-medium">(selected)</span>}
                    </td>
                    <td className="td">{principalText(r.principal)}</td>
                    <td className="td">
                      {r.run_id ? (
                        <Link
                          className="text-blue-800 underline"
                          href={`${orgHref(org)}/runs/${r.run_id}`}
                        >
                          {r.run_id}
                        </Link>
                      ) : (
                        '-'
                      )}
                    </td>
                    <td className="td font-mono text-xs">{(r.object_types ?? []).join(', ')}</td>
                    <td className="td font-mono text-xs">{(r.connections ?? []).join(', ')}</td>
                    <td className="td">{countsText(r.row_counts)}</td>
                    <td className="td">
                      {r.duration_ms !== undefined ? `${r.duration_ms} ms` : ''}
                    </td>
                    <td className="td">
                      <ul className="space-y-0.5">
                        {(r.compiled_query_hashes ?? []).map((h) => (
                          <li key={h} className="font-mono text-xs" title={h}>
                            {h.length > 16 ? `${h.slice(0, 16)}...` : h}
                          </li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
      {q.hasNextPage && (
        <button
          type="button"
          className="btn"
          disabled={q.isFetchingNextPage}
          onClick={() => void q.fetchNextPage()}
        >
          {q.isFetchingNextPage ? 'Loading...' : 'Load more'}
        </button>
      )}
    </div>
  );
}
