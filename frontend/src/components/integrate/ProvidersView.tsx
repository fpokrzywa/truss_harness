'use client';

import Link from 'next/link';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { useCan } from '@/lib/hooks';
import { useAdapters, useConnections, useProviders, useRefreshInt } from '@/lib/integrate-hooks';
import { isPlannedAdapter } from '@/lib/maturity';
import {
  ActionError,
  EmptyState,
  formatDate,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { StatusBadge } from './Maturity';

/** Adapters that may back a model provider (contract section 3). */
export const PROVIDER_ADAPTERS = ['openai_compatible', 'anthropic', 'fake'];

export function ProvidersView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const providers = useProviders(org);
  const [creating, setCreating] = useState(false);
  const canManage = can('model:manage');

  return (
    <div className="space-y-6">
      <PageHeader
        title="Model providers"
        description="A provider binds a model adapter to a model_api connection that holds the endpoint and credential."
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              Create provider
            </button>
          )
        }
      />
      <QueryGate query={providers} noun="providers">
        {(data) => (
          <section aria-label="Providers" className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>No providers yet.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Model providers</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Name', 'Adapter', 'Health', 'Last checked'].map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((p) => (
                    <tr key={p.id}>
                      <td className="td font-medium">
                        <Link
                          className="text-blue-800 underline"
                          href={`/o/${encodeURIComponent(org)}/integrate/providers/${encodeURIComponent(p.id)}`}
                        >
                          {p.name}
                        </Link>
                      </td>
                      <td className="td font-mono">{p.adapter_key}</td>
                      <td className="td">
                        <StatusBadge status={p.health_status ?? 'unknown'} />
                      </td>
                      <td className="td">{formatDate(p.health_checked_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}
      </QueryGate>
      {creating && <CreateProviderDialog org={org} onClose={() => setCreating(false)} />}
    </div>
  );
}

function CreateProviderDialog({ org, onClose }: { org: string; onClose: () => void }) {
  const refresh = useRefreshInt(org);
  const adapters = useAdapters();
  const connections = useConnections(org, { type: 'model_api' });
  const action = useAction();
  const [name, setName] = useState('');
  const [adapterKey, setAdapterKey] = useState('');
  const [connectionId, setConnectionId] = useState('');
  const [errors, setErrors] = useState<{ name?: string; adapter?: string; connection?: string }>(
    {},
  );

  const options = (adapters.data ?? []).filter((a) => PROVIDER_ADAPTERS.includes(a.key));

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next = {
      name: name.trim() === '' ? 'Name is required.' : undefined,
      adapter: adapterKey === '' ? 'Choose an adapter.' : undefined,
      connection: connectionId === '' ? 'Choose a model_api connection.' : undefined,
    };
    setErrors(next);
    if (next.name || next.adapter || next.connection) return;
    const res = await action.run(() =>
      endpoints.createProvider(org, {
        name: name.trim(),
        adapter_key: adapterKey,
        connection_id: connectionId,
      }),
    );
    if (res.ok) {
      await refresh('providers');
      onClose();
    }
  }

  return (
    <Modal title="Create provider" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField
          label="Name"
          value={name}
          onChange={setName}
          error={errors.name}
          maxLength={200}
        />
        <div>
          <SelectField
            label="Adapter"
            value={adapterKey}
            onChange={setAdapterKey}
            options={[
              { value: '', label: 'Select an adapter...' },
              ...options.map((a) => ({
                value: a.key,
                label: isPlannedAdapter(a) ? `${a.key} (declared only)` : a.key,
              })),
            ]}
          />
          {errors.adapter && <p className="mt-1 text-sm text-red-700">{errors.adapter}</p>}
        </div>
        <div>
          <SelectField
            label="Model API connection"
            value={connectionId}
            onChange={setConnectionId}
            options={[
              { value: '', label: 'Select a connection...' },
              ...(connections.data?.items ?? []).map((c) => ({ value: c.id, label: c.name })),
            ]}
          />
          {errors.connection && <p className="mt-1 text-sm text-red-700">{errors.connection}</p>}
        </div>
        <ActionError error={action.error} title="Could not create the provider" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Create provider" />
      </form>
    </Modal>
  );
}
