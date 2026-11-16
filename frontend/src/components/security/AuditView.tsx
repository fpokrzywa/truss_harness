'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { AuditEvent, AuditFilters, AuditVerifyResult } from '@/lib/api/security-types';
import { QueryError } from '../QueryError';
import {
  ActionError,
  EmptyState,
  formatDate,
  PageHeader,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from './useOrg';

type Filters = Omit<AuditFilters, 'cursor'>;

/** `datetime-local` value (local time, no zone) to an ISO-8601 UTC instant; empty stays unset. */
export function toIso(local: string): string | undefined {
  if (local === '') return undefined;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

/** Categories offered by the Audit Explorer (assumption: the server accepts these on `category=`). */
export const AUDIT_CATEGORIES = [
  'security',
  'tenancy',
  'integrate',
  'build',
  'operate',
  'pipelines',
  'ontology',
  'governance',
] as const;

/**
 * Security audit (default: `category=security`) and, with `explorer`, the Phase 8 Audit Explorer: the same
 * table, filters, paging and chain verification across every category with a category filter.
 */
export function AuditView({ explorer = false }: { explorer?: boolean }) {
  const org = useOrgParam();
  const [category, setCategory] = useState('');
  const [actor, setActor] = useState('');
  const [action, setAction] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [applied, setApplied] = useState<Filters>(explorer ? { category: undefined } : {});
  const verify = useAction();
  const [verifyResult, setVerifyResult] = useState<AuditVerifyResult | null>(null);

  const events = useInfiniteQuery({
    queryKey: ['audit', org, applied],
    queryFn: ({ pageParam }) =>
      endpoints.listAuditEvents(org, { ...applied, ...(pageParam ? { cursor: pageParam } : {}) }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  function apply(e: FormEvent) {
    e.preventDefault();
    setApplied({
      ...(explorer ? { category: category || undefined } : {}),
      actor: actor.trim() || undefined,
      action: action.trim() || undefined,
      from: toIso(from),
      to: toIso(to),
    });
  }

  function clear() {
    setActor('');
    setAction('');
    setFrom('');
    setTo('');
    setCategory('');
    setApplied(explorer ? { category: undefined } : {});
  }

  async function runVerify() {
    setVerifyResult(null);
    const res = await verify.run(() => endpoints.verifyAudit(org));
    if (res.ok) setVerifyResult(res.value);
  }

  const items: AuditEvent[] = events.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title={explorer ? 'Audit explorer' : 'Security audit events'}
        description={
          explorer
            ? 'Audit events across all categories, including governance changes. The audit trail is append-only and hash-chained.'
            : 'Authentication, authorization and administration events. The audit trail is append-only and hash-chained.'
        }
        actions={
          <button
            type="button"
            className="btn"
            onClick={() => void runVerify()}
            disabled={verify.busy}
          >
            {verify.busy ? 'Verifying...' : 'Verify chain'}
          </button>
        }
      />
      {verifyResult && (
        <p
          role="status"
          className={`rounded-md border p-3 text-sm ${
            verifyResult.ok
              ? 'border-green-300 bg-green-50 text-green-900'
              : 'border-red-300 bg-red-50 text-red-900'
          }`}
        >
          {verifyResult.ok
            ? `Chain verified: ${verifyResult.checked} events checked, no tampering detected.`
            : `Chain verification FAILED after ${verifyResult.checked} events.${
                verifyResult.first_broken_id
                  ? ` First broken event: ${verifyResult.first_broken_id}.`
                  : ''
              }`}
        </p>
      )}
      <ActionError error={verify.error} title="Could not verify the audit chain" />

      <form
        onSubmit={apply}
        aria-label="Audit filters"
        className="card grid gap-3 sm:grid-cols-2 lg:grid-cols-4"
      >
        {explorer && (
          <SelectField
            label="Category"
            value={category}
            onChange={setCategory}
            options={[
              { value: '', label: 'All categories' },
              ...AUDIT_CATEGORIES.map((c) => ({ value: c, label: c })),
            ]}
          />
        )}
        <TextField label="Actor" value={actor} onChange={setActor} />
        <TextField label="Action" value={action} onChange={setAction} />
        <TextField label="From" type="datetime-local" value={from} onChange={setFrom} />
        <TextField label="To" type="datetime-local" value={to} onChange={setTo} />
        <div className="flex gap-2 sm:col-span-2 lg:col-span-4">
          <button type="submit" className="btn btn-primary">
            Apply filters
          </button>
          <button type="button" className="btn" onClick={clear}>
            Clear
          </button>
        </div>
      </form>

      {events.isPending && (
        <p role="status" className="text-sm text-slate-600">
          Loading events...
        </p>
      )}
      {events.isError && <QueryError error={events.error} />}
      {events.data && (
        <section aria-label="Audit events" className="card overflow-x-auto p-0">
          {items.length === 0 ? (
            <EmptyState>
              No {explorer ? 'audit' : 'security'} events match these filters.
            </EmptyState>
          ) : (
            <table className="w-full">
              <caption className="sr-only">
                {explorer ? 'Audit events' : 'Security audit events'}
              </caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {[
                    'When',
                    ...(explorer ? ['Category'] : []),
                    'Actor',
                    'Action',
                    'Target',
                    'Outcome',
                    'Request ID',
                  ].map((h) => (
                    <th key={h} scope="col" className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {items.map((e) => (
                  <tr key={e.id}>
                    <td className="td whitespace-nowrap">
                      {formatDate(e.occurred_at ?? e.at ?? e.created_at)}
                    </td>
                    {explorer && <td className="td">{e.category ?? '-'}</td>}
                    <td className="td">{e.actor ?? '-'}</td>
                    <td className="td font-mono text-xs">{e.action}</td>
                    <td className="td">{e.target ?? '-'}</td>
                    <td className="td">{e.outcome ?? '-'}</td>
                    <td className="td font-mono text-xs">{e.request_id ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {events.hasNextPage && (
            <div className="border-t border-slate-200 p-3">
              <button
                type="button"
                className="btn"
                onClick={() => void events.fetchNextPage()}
                disabled={events.isFetchingNextPage}
              >
                {events.isFetchingNextPage ? 'Loading...' : 'Load more'}
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
