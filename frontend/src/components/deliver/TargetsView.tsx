'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { DeliveryEnvironment, TargetOut } from '@/lib/api/delivery-types';
import { useRefreshDelivery, useTargets } from '@/lib/delivery-hooks';
import { useCan, useProjects, useWorkspaces } from '@/lib/hooks';
import { ConfirmDialog } from '../ConfirmDialog';
import {
  ActionError,
  EmptyState,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { MaturityBadge, StatusBadge } from '../integrate/Maturity';
import { useOrgParam } from '../security/useOrg';

const COLUMNS = [
  'Name',
  'Kind',
  'Address',
  'Environment',
  'Workspace / project',
  'Warehouse ID',
  'Serving endpoint',
  'Credential reference',
  'Status',
];
const SERVING_ENDPOINT = /^[A-Za-z0-9_-]{1,63}$/;

/** Deployment targets: list, register, archive. Server text renders as plain React text only. */
export function TargetsView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const targets = useTargets(org);
  const refresh = useRefreshDelivery(org);
  const archive = useAction();
  const [registering, setRegistering] = useState(false);
  const [archiving, setArchiving] = useState<TargetOut | null>(null);
  const canWrite = can('delivery:target:write');

  async function confirmArchive() {
    if (!archiving) return;
    const res = await archive.run(() => endpoints.archiveTarget(org, archiving.id));
    setArchiving(null);
    if (res.ok) await refresh('targets');
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Deployment targets"
        description="Platforms this organization deploys to. Each platform address can belong to one organization only."
        actions={
          canWrite && (
            <button type="button" className="btn btn-primary" onClick={() => setRegistering(true)}>
              Register target
            </button>
          )
        }
      />
      <ActionError error={archive.error} title="Could not archive the target" />
      <QueryGate query={targets} noun="targets">
        {(data) => (
          <div className="card overflow-x-auto p-0">
            {data.items.length === 0 ? (
              <EmptyState>
                No deployment targets yet.
                {canWrite && ' Use "Register target" to add the first one.'}
              </EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Deployment targets</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {COLUMNS.map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                    <th scope="col" className="th">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((t) => (
                    <tr key={t.id}>
                      <td className="td font-medium">{t.name}</td>
                      <td className="td">
                        <span className="flex flex-wrap items-center gap-2">
                          {t.platform_kind === 'databricks' ? 'Databricks' : t.platform_kind}
                          {t.platform_kind === 'databricks' && (
                            <MaturityBadge maturity="experimental" />
                          )}
                        </span>
                      </td>
                      <td className="td font-mono text-xs break-all">{t.address}</td>
                      <td className="td">{t.environment}</td>
                      <td className="td">
                        {t.workspace ?? '-'}
                        {t.project && <> / {t.project}</>}
                      </td>
                      <td className="td font-mono text-xs">{t.bindings.warehouse_id}</td>
                      <td className="td font-mono text-xs">{t.bindings.serving_endpoint || '-'}</td>
                      <td className="td font-mono text-xs">{t.credential_ref}</td>
                      <td className="td">
                        <StatusBadge status={t.status} />
                      </td>
                      <td className="td">
                        {canWrite && t.status === 'active' && (
                          <div className="flex justify-end">
                            <button type="button" className="btn" onClick={() => setArchiving(t)}>
                              Archive <span className="sr-only">{t.name}</span>
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </QueryGate>

      {registering && <RegisterTargetDialog org={org} onClose={() => setRegistering(false)} />}
      <ConfirmDialog
        open={archiving !== null}
        title="Archive target?"
        confirmLabel="Archive target"
        busy={archive.busy}
        onConfirm={() => void confirmArchive()}
        onCancel={() => setArchiving(null)}
      >
        <p>
          <strong>{archiving?.name}</strong> can no longer be used for new plans or deployments.
          This cannot be undone.
        </p>
      </ConfirmDialog>
    </div>
  );
}

function RegisterTargetDialog({ org, onClose }: { org: string; onClose: () => void }) {
  const workspaces = useWorkspaces(org);
  const refresh = useRefreshDelivery(org);
  const action = useAction();
  const [name, setName] = useState('');
  const [workspace, setWorkspace] = useState('');
  const [project, setProject] = useState('');
  const [address, setAddress] = useState('');
  const [environment, setEnvironment] = useState<DeliveryEnvironment>('dev');
  const [warehouseId, setWarehouseId] = useState('');
  const [servingEndpoint, setServingEndpoint] = useState('');
  const [credentialRef, setCredentialRef] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const projects = useProjects(org, workspace || undefined);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (name.trim() === '') next.name = 'Name is required.';
    if (workspace === '') next.workspace = 'Choose a workspace.';
    if (address.trim() === '') next.address = 'Address is required.';
    if (warehouseId.trim() === '') next.warehouseId = 'Warehouse ID is required.';
    const endpoint = servingEndpoint.trim();
    if (endpoint !== '' && !SERVING_ENDPOINT.test(endpoint)) {
      next.servingEndpoint = 'Use 1-63 letters, digits, underscores or hyphens.';
    }
    if (credentialRef.trim() === '') next.credentialRef = 'Credential reference is required.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const res = await action.run(() =>
      endpoints.registerTarget(org, {
        workspace,
        ...(project ? { project } : {}),
        name: name.trim(),
        platform_kind: 'databricks',
        address: address.trim(),
        environment,
        bindings: {
          warehouse_id: warehouseId.trim(),
          ...(endpoint ? { serving_endpoint: endpoint } : {}),
        },
        credential_ref: credentialRef.trim(),
      }),
    );
    if (res.ok) {
      await refresh('targets');
      onClose();
    }
  }

  const workspaceOptions = (workspaces.data?.items ?? [])
    .filter((w) => w.status === 'active')
    .map((w) => ({ value: w.slug, label: w.name }));
  const projectOptions = workspace
    ? (projects.data?.items ?? [])
        .filter((p) => p.status === 'active')
        .map((p) => ({ value: p.slug, label: p.name }))
    : [];

  return (
    <Modal title="Register target" wide onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField
          label="Name"
          value={name}
          onChange={setName}
          error={errors.name}
          maxLength={200}
        />
        <p className="text-sm text-slate-700">
          Platform kind: <strong>Databricks</strong> <MaturityBadge maturity="experimental" />
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <SelectField
              label="Workspace"
              value={workspace}
              onChange={(v) => {
                setWorkspace(v);
                setProject('');
              }}
              options={[{ value: '', label: 'Select...' }, ...workspaceOptions]}
            />
            {errors.workspace && <p className="mt-1 text-sm text-red-700">{errors.workspace}</p>}
          </div>
          <SelectField
            label="Project (optional)"
            value={project}
            onChange={setProject}
            disabled={workspace === ''}
            options={[{ value: '', label: 'None (whole workspace)' }, ...projectOptions]}
          />
        </div>
        <TextField
          label="Address"
          value={address}
          onChange={setAddress}
          error={errors.address}
          maxLength={2048}
          monospace
          help="The Databricks workspace URL."
        />
        <SelectField
          label="Environment"
          value={environment}
          onChange={(v) => setEnvironment(v as DeliveryEnvironment)}
          options={[
            { value: 'dev', label: 'dev' },
            { value: 'test', label: 'test' },
            { value: 'prod', label: 'prod' },
          ]}
        />
        <div className="grid gap-3 sm:grid-cols-2">
          <TextField
            label="Warehouse ID"
            value={warehouseId}
            onChange={setWarehouseId}
            error={errors.warehouseId}
            maxLength={64}
            monospace
          />
          <TextField
            label="Serving endpoint (optional)"
            value={servingEndpoint}
            onChange={setServingEndpoint}
            error={errors.servingEndpoint}
            maxLength={63}
            monospace
            help="The model serving endpoint a generated app queries. Needed to deploy a generated app."
          />
        </div>
        <TextField
          label="Credential reference"
          value={credentialRef}
          onChange={setCredentialRef}
          error={errors.credentialRef}
          maxLength={128}
          monospace
          help="The name the executor resolves to a platform credential."
        />
        <ActionError error={action.error} title="Could not register the target" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Register target" />
      </form>
    </Modal>
  );
}
