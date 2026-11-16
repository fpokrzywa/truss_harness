'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { useCan } from '@/lib/hooks';
import { securityHref } from '@/lib/scope';
import { useRefresh, useServiceAccounts } from '@/lib/security-hooks';
import { validateName } from '@/lib/validation';
import { ConfirmDialog } from '../ConfirmDialog';
import { NoAccess } from '../NoAccess';
import { RoleBindings } from '../admin/RoleBindings';
import {
  ActionError,
  Badge,
  EmptyState,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { ApiKeysPanel } from './ApiKeysPanel';
import { useOrgParam } from './useOrg';

export function ServiceAccountsView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const accounts = useServiceAccounts(org);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Service accounts"
        description="Non-human principals for automation. Their API keys act with the roles assigned here."
      />
      <QueryGate query={accounts} noun="service accounts">
        {(data) => (
          <section aria-label="Service accounts" className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>No service accounts yet.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Service accounts</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    <th scope="col" className="th">
                      Name
                    </th>
                    <th scope="col" className="th">
                      Status
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((a) => (
                    <tr key={a.id}>
                      <td className="td">
                        <Link
                          href={securityHref(org, `service-accounts/${encodeURIComponent(a.id)}`)}
                          className="font-medium text-blue-800 underline"
                        >
                          {a.name}
                        </Link>
                        {a.description && <p className="text-xs text-slate-600">{a.description}</p>}
                      </td>
                      <td className="td">
                        <Badge tone={a.status === 'active' ? 'good' : 'warn'}>
                          {a.status === 'active' ? 'Active' : 'Disabled'}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}
      </QueryGate>
      {can('serviceaccount:manage') && <CreateServiceAccountForm org={org} />}
    </div>
  );
}

function CreateServiceAccountForm({ org }: { org: string }) {
  const refresh = useRefresh(org);
  const action = useAction();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [nameError, setNameError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const err = validateName(name);
    setNameError(err);
    if (err) return;
    const res = await action.run(() =>
      endpoints.createServiceAccount(org, {
        name: name.trim(),
        ...(description.trim() ? { description: description.trim() } : {}),
      }),
    );
    if (res.ok) {
      setName('');
      setDescription('');
      await refresh('service-accounts');
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="card space-y-4" noValidate>
      <h2 className="text-base font-semibold text-slate-900">Create service account</h2>
      <TextField label="Name" value={name} onChange={setName} error={nameError} maxLength={200} />
      <TextField
        label="Description (optional)"
        value={description}
        onChange={setDescription}
        multiline
      />
      <ActionError error={action.error} title="Could not create the service account" />
      <button type="submit" className="btn btn-primary" disabled={action.busy}>
        {action.busy ? 'Creating...' : 'Create service account'}
      </button>
    </form>
  );
}

export function ServiceAccountDetailView() {
  const org = useOrgParam();
  const id = decodeURIComponent(useParams<{ id: string }>().id);
  const { can } = useCan(org);
  const accounts = useServiceAccounts(org);
  const refresh = useRefresh(org);
  const disable = useAction();
  const [confirming, setConfirming] = useState(false);

  return (
    <QueryGate query={accounts} noun="service account">
      {(data) => {
        // The contract has no single-item GET; find it in the (first-page) list.
        const sa = data.items.find((a) => a.id === id);
        if (!sa) return <NoAccess />;
        async function confirmDisable() {
          if (!sa) return;
          const res = await disable.run(() =>
            endpoints.setServiceAccountStatus(org, sa, 'disabled'),
          );
          setConfirming(false);
          if (res.ok) await refresh('service-accounts', 'api-keys');
        }
        return (
          <div className="space-y-6">
            <p className="text-sm">
              <Link
                href={securityHref(org, 'service-accounts')}
                className="text-blue-800 underline"
              >
                Back to service accounts
              </Link>
            </p>
            <PageHeader
              title={sa.name}
              description={sa.description ?? undefined}
              actions={
                <>
                  <Badge tone={sa.status === 'active' ? 'good' : 'warn'}>
                    {sa.status === 'active' ? 'Active' : 'Disabled'}
                  </Badge>
                  {can('serviceaccount:manage') && sa.status === 'active' && (
                    <button type="button" className="btn" onClick={() => setConfirming(true)}>
                      Disable
                    </button>
                  )}
                </>
              }
            />
            <ActionError error={disable.error} title="Could not disable the service account" />
            <ApiKeysPanel
              org={org}
              filters={{ principal_type: 'service_account', principal_id: sa.id }}
              canCreate={sa.status === 'active'}
              fixedPrincipal={{ type: 'service_account', id: sa.id, label: sa.name }}
            />
            {can('role:read') && (
              <RoleBindings
                org={org}
                subjectType="service_account"
                subjectId={sa.id}
                canManage={can('role:manage')}
              />
            )}
            <ConfirmDialog
              open={confirming}
              title="Disable service account?"
              confirmLabel="Disable account"
              busy={disable.busy}
              onConfirm={() => void confirmDisable()}
              onCancel={() => setConfirming(false)}
            >
              <p>
                <strong>{sa.name}</strong> is disabled and all of its API keys are revoked
                immediately.
              </p>
            </ConfirmDialog>
          </div>
        );
      }}
    </QueryGate>
  );
}
