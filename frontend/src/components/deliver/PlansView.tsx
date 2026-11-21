'use client';

import Link from 'next/link';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { usePlans, useRefreshDelivery, useTargets } from '@/lib/delivery-hooks';
import { useCan } from '@/lib/hooks';
import { deliverHref } from '@/lib/scope';
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
import { StatusBadge } from '../integrate/Maturity';
import { useOrgParam } from '../security/useOrg';
import { targetName } from './targetName';

const COLUMNS = ['Display name', 'Application key', 'Target', 'Status', 'Expires', 'Created'];
const APPLICATION_KEY = /^[a-z][a-z0-9-]{0,62}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Deployment plans: list and request. Server text renders as plain React text only. */
export function PlansView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const plans = usePlans(org);
  const targets = useTargets(org);
  const [requesting, setRequesting] = useState(false);
  const canRequest = can('delivery:plan:request');

  return (
    <div className="space-y-6">
      <PageHeader
        title="Deployment plans"
        description="A plan is sealed against a target, then approved by a second person before it deploys."
        actions={
          canRequest && (
            <button type="button" className="btn btn-primary" onClick={() => setRequesting(true)}>
              Request plan
            </button>
          )
        }
      />
      <QueryGate query={plans} noun="plans">
        {(data) => (
          <div className="card overflow-x-auto p-0">
            {data.items.length === 0 ? (
              <EmptyState>
                No deployment plans yet.
                {canRequest && ' Use "Request plan" to create the first one.'}
              </EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Deployment plans</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {COLUMNS.map((h) => (
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
                          href={deliverHref(org, `plans/${encodeURIComponent(p.id)}`)}
                        >
                          {p.display_name}
                        </Link>
                      </td>
                      <td className="td font-mono text-xs">{p.application_key}</td>
                      <td className="td">{targetName(targets.data?.items, p.target_id)}</td>
                      <td className="td">
                        <StatusBadge status={p.status} />
                      </td>
                      <td className="td">{formatDate(p.expires_at)}</td>
                      <td className="td">{formatDate(p.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </QueryGate>

      {requesting && <RequestPlanDialog org={org} onClose={() => setRequesting(false)} />}
    </div>
  );
}

function RequestPlanDialog({ org, onClose }: { org: string; onClose: () => void }) {
  const targets = useTargets(org);
  const refresh = useRefreshDelivery(org);
  const action = useAction();
  const [targetId, setTargetId] = useState('');
  const [releaseId, setReleaseId] = useState('');
  const [applicationKey, setApplicationKey] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    const name = displayName.trim();
    const release = releaseId.trim();
    if (targetId === '') next.target = 'Choose a target.';
    if (!UUID.test(release)) next.releaseId = 'Enter the release id.';
    if (!APPLICATION_KEY.test(applicationKey)) {
      next.applicationKey =
        'Use 1-63 lowercase letters, digits or hyphens, starting with a letter.';
    }
    if (name === '' || name.length > 200) next.displayName = 'Display name is 1-200 characters.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const res = await action.run(() =>
      endpoints.requestPlan(org, {
        target_id: targetId,
        release_id: release,
        application_key: applicationKey,
        display_name: name,
      }),
    );
    if (res.ok) {
      await refresh('plans');
      onClose();
    }
  }

  const targetOptions = (targets.data?.items ?? [])
    .filter((t) => t.status === 'active')
    .map((t) => ({ value: t.id, label: t.name }));

  return (
    <Modal title="Request plan" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <div>
          <SelectField
            label="Target"
            value={targetId}
            onChange={setTargetId}
            options={[{ value: '', label: 'Select...' }, ...targetOptions]}
          />
          {errors.target && <p className="mt-1 text-sm text-red-700">{errors.target}</p>}
        </div>
        <TextField
          label="Release id"
          value={releaseId}
          onChange={setReleaseId}
          error={errors.releaseId}
          maxLength={36}
          monospace
        />
        <TextField
          label="Application key"
          value={applicationKey}
          onChange={setApplicationKey}
          error={errors.applicationKey}
          maxLength={63}
          monospace
        />
        <TextField
          label="Display name"
          value={displayName}
          onChange={setDisplayName}
          error={errors.displayName}
          maxLength={200}
        />
        <ActionError error={action.error} title="Could not request the plan" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Request plan" />
      </form>
    </Modal>
  );
}
