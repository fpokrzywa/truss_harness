'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState, type FormEvent } from 'react';
import { REQUEST_STATUSES, type RequestStatus } from '@/lib/api/appgen-types';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import type { Connection } from '@/lib/api/integrate-types';
import { useAppRequests } from '@/lib/appgen-hooks';
import { normalizeDiscovery, type DiscoveredTable } from '@/lib/discovery';
import { useCan } from '@/lib/hooks';
import { useConnections, useEffectiveModels, useLatestDiscovery } from '@/lib/integrate-hooks';
import { appRequestHref, connectionHref, connectionsHref } from '@/lib/scope';
import { Tabs } from '../command/Tabs';
import { ErrorMessage } from '../ErrorMessage';
import {
  ActionError,
  EmptyState,
  formatDate,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { StatusBadge } from '../integrate/Maturity';
import { ProjectScopePicker, useProjectQuery } from '../releases/ProjectScope';
import { useOrgParam } from '../security/useOrg';

const MAX_TABLES = 50;
const MAX_BRIEF = 4000;

/** Only an active, read-only Databricks connection can feed the generator (design SP5 4). */
const eligible = (c: Connection) =>
  c.adapter_key === 'databricks' && c.access_mode === 'read' && c.status === 'active';

const tableName = (t: DiscoveredTable) =>
  [t.catalog, t.schema, t.name].filter((p) => p !== undefined && p !== '').join('.');

/** The solution generator: a new app request and the project's requests. Plain React text only. */
export function GenerateView() {
  const org = useOrgParam();
  const { ws, proj } = useProjectQuery();
  const { can, loaded } = useCan(org);
  const canWrite = can('appgen:request:write');
  const chosen = ws !== '' && proj !== '';

  return (
    <div className="space-y-6">
      <PageHeader
        title="Generate solution"
        description="Describe an app over a project's Databricks tables: a model drafts its agent, tools, screens and evaluation cases, and you review and accept the draft as a solution."
      />
      <ProjectScopePicker org={org} />
      {!chosen ? (
        <div className="card p-0">
          <EmptyState>Choose a workspace and a project to generate a solution.</EmptyState>
        </div>
      ) : !loaded ? (
        <p role="status" className="text-sm text-slate-600">
          Loading permissions...
        </p>
      ) : (
        <>
          {!canWrite && (
            <p className="text-sm text-slate-700">
              You can view this project&apos;s app requests but not create them.
            </p>
          )}
          <Tabs
            key={canWrite ? 'write' : 'read'}
            label="Solution generator"
            tabs={[
              ...(canWrite
                ? [
                    {
                      id: 'new',
                      label: 'New request',
                      content: <NewRequestForm org={org} ws={ws} proj={proj} />,
                    },
                  ]
                : []),
              {
                id: 'requests',
                label: 'Requests',
                content: <RequestsList org={org} ws={ws} proj={proj} />,
              },
            ]}
          />
        </>
      )}
    </div>
  );
}

interface Scope {
  org: string;
  ws: string;
  proj: string;
}

function NewRequestForm({ org, ws, proj }: Scope) {
  const router = useRouter();
  const connections = useConnections(org, { workspace: ws, project: proj, include_shared: 'true' });
  const models = useEffectiveModels(org, ws, proj);
  const action = useAction();
  const [connectionId, setConnectionId] = useState('');
  const [tables, setTables] = useState<string[]>([]);
  const [brief, setBrief] = useState('');
  const [modelId, setModelId] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (connectionId === '') next.connection = 'Choose a connection.';
    if (connectionId !== '' && tables.length === 0) next.tables = 'Choose at least one table.';
    const text = brief.trim();
    if (text === '' || text.length > MAX_BRIEF)
      next.brief = `The brief must be 1 to ${MAX_BRIEF} characters.`;
    if (modelId === '') next.model = 'Choose a drafting model.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const res = await action.run(() =>
      endpoints.createAppRequest(org, ws, proj, {
        connection_id: connectionId,
        tables,
        brief: text,
        model_id: modelId,
      }),
    );
    if (res.ok) router.push(appRequestHref(org, res.value.id, ws, proj));
  }

  return (
    <QueryGate query={connections} noun="connections">
      {(data) => {
        const options = data.items.filter(eligible);
        if (options.length === 0)
          return (
            <div className="card p-0">
              <EmptyState>
                Generating a solution needs an active, read-only Databricks connection in this
                project. Add one on the{' '}
                <Link className="text-blue-800 underline" href={connectionsHref(org, ws, proj)}>
                  Connections
                </Link>{' '}
                screen.
              </EmptyState>
            </div>
          );
        return (
          <form
            onSubmit={(e) => void submit(e)}
            className="card max-w-3xl space-y-4"
            aria-label="New app request"
            noValidate
          >
            <div>
              <SelectField
                label="Connection"
                value={connectionId}
                onChange={(v) => {
                  setConnectionId(v);
                  setTables([]);
                }}
                options={[
                  { value: '', label: 'Select...' },
                  ...options.map((c) => ({ value: c.id, label: c.name })),
                ]}
              />
              {errors.connection && (
                <p className="mt-1 text-sm text-red-700">{errors.connection}</p>
              )}
            </div>
            {connectionId !== '' && (
              <TablePicker
                key={connectionId}
                org={org}
                connectionId={connectionId}
                selected={tables}
                onChange={setTables}
                error={errors.tables}
              />
            )}
            <TextField
              label="Brief"
              value={brief}
              onChange={setBrief}
              multiline
              maxLength={MAX_BRIEF}
              help="What the app is for and who uses it, in up to 4000 characters."
              error={errors.brief}
            />
            <div>
              <SelectField
                label="Drafting model"
                value={modelId}
                onChange={setModelId}
                options={[
                  { value: '', label: 'Select...' },
                  ...(models.data?.items ?? []).map((m) => ({
                    value: m.id,
                    label: m.display_name,
                  })),
                ]}
              />
              {errors.model && <p className="mt-1 text-sm text-red-700">{errors.model}</p>}
            </div>
            <ActionError error={action.error} title="Could not start the draft" />
            <div className="flex justify-end">
              <button type="submit" className="btn btn-primary" disabled={action.busy}>
                {action.busy ? 'Working...' : 'Generate draft'}
              </button>
            </div>
          </form>
        );
      }}
    </QueryGate>
  );
}

function TablePicker({
  org,
  connectionId,
  selected,
  onChange,
  error,
}: {
  org: string;
  connectionId: string;
  selected: string[];
  onChange: (tables: string[]) => void;
  error?: string;
}) {
  const discovery = useLatestDiscovery(org, connectionId);
  if (discovery.isPending)
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading tables...
      </p>
    );
  const notFound =
    discovery.isError && discovery.error instanceof ApiError && discovery.error.status === 404;
  if (discovery.isError && !notFound) return <ErrorMessage error={discovery.error} />;
  const names = discovery.isSuccess ? normalizeDiscovery(discovery.data).tables.map(tableName) : [];
  if (names.length === 0)
    return (
      <p className="text-sm text-slate-700">
        This connection has no discovered tables; run a discovery first on the{' '}
        <Link className="text-blue-800 underline" href={connectionHref(org, connectionId)}>
          connection
        </Link>
        .
      </p>
    );

  const full = selected.length >= MAX_TABLES;
  const toggle = (value: string, on: boolean) =>
    onChange(on ? [...selected, value] : selected.filter((t) => t !== value));

  return (
    <fieldset>
      <legend className="label">Tables</legend>
      <p className="mb-2 text-xs text-slate-600">
        {selected.length} of at most {MAX_TABLES} selected.
      </p>
      <div className="max-h-64 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
        {names.map((name) => {
          const value = name.toLowerCase();
          const checked = selected.includes(value);
          return (
            <label key={name} className="flex items-center gap-2 font-mono text-xs">
              <input
                type="checkbox"
                checked={checked}
                disabled={!checked && full}
                onChange={(e) => toggle(value, e.target.checked)}
              />
              {name}
            </label>
          );
        })}
      </div>
      {error && <p className="mt-1 text-sm text-red-700">{error}</p>}
    </fieldset>
  );
}

const STATUS_OPTIONS = [
  { value: '', label: 'All' },
  ...REQUEST_STATUSES.map((s) => ({ value: s, label: s })),
];

function RequestsList({ org, ws, proj }: Scope) {
  const [status, setStatus] = useState<RequestStatus | ''>('');
  const requests = useAppRequests(org, ws, proj, status);

  return (
    <div className="space-y-4">
      <div className="max-w-xs">
        <SelectField
          label="Status"
          value={status}
          onChange={(v) => setStatus(v as RequestStatus | '')}
          options={STATUS_OPTIONS}
        />
      </div>
      <QueryGate query={requests} noun="app requests">
        {(data) => {
          const items = data.pages.flatMap((p) => p.items);
          return (
            <div className="card overflow-x-auto p-0">
              {items.length === 0 ? (
                <EmptyState>No app requests in this project yet.</EmptyState>
              ) : (
                <table className="w-full">
                  <caption className="sr-only">App requests</caption>
                  <thead className="border-b border-slate-200">
                    <tr>
                      {['Status', 'Reason', 'Latest revision', 'Created'].map((h) => (
                        <th key={h} scope="col" className="th">
                          {h}
                        </th>
                      ))}
                      <th scope="col" className="th">
                        <span className="sr-only">Request</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {items.map((r) => (
                      <tr key={r.id}>
                        <td className="td">
                          <StatusBadge status={r.status} />
                        </td>
                        <td className="td">{r.status_reason ?? '-'}</td>
                        <td className="td">
                          {r.latest_revision_no === null ? '-' : `r${r.latest_revision_no}`}
                        </td>
                        <td className="td">{formatDate(r.created_at)}</td>
                        <td className="td">
                          <Link
                            className="text-blue-800 underline"
                            href={appRequestHref(org, r.id, ws, proj)}
                          >
                            Open{' '}
                            <span className="sr-only">
                              request created {formatDate(r.created_at)}
                            </span>
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {requests.hasNextPage && (
                <div className="border-t border-slate-200 p-3">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void requests.fetchNextPage()}
                    disabled={requests.isFetchingNextPage}
                  >
                    {requests.isFetchingNextPage ? 'Loading...' : 'Load more'}
                  </button>
                </div>
              )}
            </div>
          );
        }}
      </QueryGate>
    </div>
  );
}
