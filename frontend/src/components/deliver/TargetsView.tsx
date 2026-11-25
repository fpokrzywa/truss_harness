'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type {
  DeliveryEnvironment,
  PlatformBindings,
  PlatformKind,
  TargetOut,
} from '@/lib/api/delivery-types';
import { useRefreshDelivery, useTargets } from '@/lib/delivery-hooks';
import { useCan, useProjects, useWorkspaces } from '@/lib/hooks';
import { platformLabel } from '@/lib/platforms';
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
  'Warehouse',
  'Serving endpoint',
  'Package',
  'Application',
  'Cortex model',
  'Credential reference',
  'Status',
];
const PLATFORMS: PlatformKind[] = ['databricks', 'snowflake'];
const SERVING_ENDPOINT = /^[A-Za-z0-9_-]{1,63}$/;
const SNOWFLAKE_IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_$]{0,254}$/;
const CORTEX_MODEL = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const IDENTIFIER_RULE = 'Use 1-255 letters, digits, _ or $, starting with a letter or _.';
const CORTEX_MODEL_RULE =
  'Use 1-64 lowercase letters, digits, dots, underscores or hyphens, starting with a letter or digit.';

/** The Warehouse to Cortex model cells; `-` where the target's platform has no such binding. */
const bindingCells = (t: TargetOut): string[] =>
  t.platform_kind === 'snowflake'
    ? [
        t.bindings.warehouse,
        '-',
        t.bindings.package,
        t.bindings.application,
        t.bindings.cortex_model,
      ]
    : [t.bindings.warehouse_id, t.bindings.serving_endpoint || '-', '-', '-', '-'];

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
                          {platformLabel(t.platform_kind)}
                          <MaturityBadge maturity="experimental" />
                        </span>
                      </td>
                      <td className="td font-mono text-xs break-all">{t.address}</td>
                      <td className="td">{t.environment}</td>
                      <td className="td">
                        {t.workspace ?? '-'}
                        {t.project && <> / {t.project}</>}
                      </td>
                      {bindingCells(t).map((value, i) => (
                        <td key={i} className="td font-mono text-xs">
                          {value}
                        </td>
                      ))}
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
  const [platform, setPlatform] = useState<PlatformKind>('databricks');
  const [address, setAddress] = useState('');
  const [environment, setEnvironment] = useState<DeliveryEnvironment>('dev');
  const [warehouseId, setWarehouseId] = useState('');
  const [servingEndpoint, setServingEndpoint] = useState('');
  const [accountAddress, setAccountAddress] = useState('');
  const [packageName, setPackageName] = useState('');
  const [application, setApplication] = useState('');
  const [warehouse, setWarehouse] = useState('');
  const [cortexModel, setCortexModel] = useState('');
  const [credentialRef, setCredentialRef] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const projects = useProjects(org, workspace || undefined);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (name.trim() === '') next.name = 'Name is required.';
    if (workspace === '') next.workspace = 'Choose a workspace.';
    const endpoint = servingEndpoint.trim();
    const snowflake = {
      package: packageName.trim(),
      application: application.trim(),
      warehouse: warehouse.trim(),
      cortex_model: cortexModel.trim(),
    };
    if (platform === 'databricks') {
      if (address.trim() === '') next.address = 'Address is required.';
      if (warehouseId.trim() === '') next.warehouseId = 'Warehouse ID is required.';
      if (endpoint !== '' && !SERVING_ENDPOINT.test(endpoint)) {
        next.servingEndpoint = 'Use 1-63 letters, digits, underscores or hyphens.';
      }
    } else {
      if (accountAddress.trim() === '') next.accountAddress = 'Account address is required.';
      if (snowflake.package === '') next.package = 'Package is required.';
      else if (!SNOWFLAKE_IDENTIFIER.test(snowflake.package)) next.package = IDENTIFIER_RULE;
      if (snowflake.application === '') next.application = 'Application is required.';
      else if (!SNOWFLAKE_IDENTIFIER.test(snowflake.application))
        next.application = IDENTIFIER_RULE;
      if (snowflake.warehouse === '') next.warehouse = 'Warehouse is required.';
      else if (!SNOWFLAKE_IDENTIFIER.test(snowflake.warehouse)) next.warehouse = IDENTIFIER_RULE;
      if (snowflake.cortex_model === '') next.cortexModel = 'Cortex model is required.';
      else if (!CORTEX_MODEL.test(snowflake.cortex_model)) next.cortexModel = CORTEX_MODEL_RULE;
    }
    if (credentialRef.trim() === '') next.credentialRef = 'Credential reference is required.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const platformBindings: PlatformBindings =
      platform === 'databricks'
        ? {
            platform_kind: 'databricks',
            bindings: {
              warehouse_id: warehouseId.trim(),
              ...(endpoint ? { serving_endpoint: endpoint } : {}),
            },
          }
        : { platform_kind: 'snowflake', bindings: snowflake };
    const res = await action.run(() =>
      endpoints.registerTarget(org, {
        workspace,
        ...(project ? { project } : {}),
        name: name.trim(),
        ...platformBindings,
        address: (platform === 'databricks' ? address : accountAddress).trim(),
        environment,
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
        <div className="flex flex-wrap items-end gap-2">
          <SelectField
            label="Platform"
            value={platform}
            disabled={action.busy}
            onChange={(v) => {
              setPlatform(v as PlatformKind);
              setErrors({});
              action.reset();
            }}
            options={PLATFORMS.map((value) => ({ value, label: platformLabel(value) }))}
          />
          <span className="pb-2">
            <MaturityBadge maturity="experimental" />
          </span>
        </div>
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
        {platform === 'databricks' ? (
          <TextField
            key="address"
            label="Address"
            value={address}
            onChange={setAddress}
            error={errors.address}
            maxLength={2048}
            monospace
            help="The Databricks workspace URL."
          />
        ) : (
          <TextField
            key="account-address"
            label="Account address"
            value={accountAddress}
            onChange={setAccountAddress}
            error={errors.accountAddress}
            maxLength={2048}
            monospace
            help="The Snowflake account URL: https://<account>.snowflakecomputing.com"
          />
        )}
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
        {platform === 'databricks' ? (
          <div key="databricks" className="grid gap-3 sm:grid-cols-2">
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
        ) : (
          <div key="snowflake" className="grid gap-3 sm:grid-cols-2">
            <TextField
              label="Package"
              value={packageName}
              onChange={setPackageName}
              error={errors.package}
              maxLength={255}
              monospace
            />
            <TextField
              label="Application"
              value={application}
              onChange={setApplication}
              error={errors.application}
              maxLength={255}
              monospace
            />
            <TextField
              label="Warehouse"
              value={warehouse}
              onChange={setWarehouse}
              error={errors.warehouse}
              maxLength={255}
              monospace
            />
            <TextField
              label="Cortex model"
              value={cortexModel}
              onChange={setCortexModel}
              error={errors.cortexModel}
              maxLength={64}
              monospace
            />
          </div>
        )}
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
