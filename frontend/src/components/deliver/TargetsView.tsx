'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { DeliveryEnvironment, PlatformKind, TargetOut } from '@/lib/api/delivery-types';
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
import {
  bindingRows,
  isKnownKind,
  isUnusable,
  PLATFORM_KINDS,
  PLATFORMS,
  platformBindings,
} from './platformKinds';

const COLUMNS = [
  'Name',
  'Kind',
  'Address',
  'Environment',
  'Workspace / project',
  'Bindings',
  'Credential reference',
  'Status',
];
const UNUSABLE_ACTIVE =
  "This target's stored settings are no longer valid, so nothing can be planned, deployed or reconciled on it. Archive it and register it again.";
const UNUSABLE_ARCHIVED = "This target's stored settings are no longer valid.";
/** The actions column stays in view while the table scrolls sideways inside its card. */
const ACTIONS_CELL =
  'sticky right-0 border-l border-slate-200 bg-slate-100 shadow-[-6px_0_6px_-4px_rgb(0_0_0/0.35)]';

/** The bindings of the row's own kind, labelled; a hint for unusable ones, `-` for an unknown kind. */
function BindingsCell({ target: t }: { target: TargetOut }) {
  if (!isKnownKind(t.platform_kind)) return <>-</>;
  if (isUnusable(t.bindings)) {
    return <p>{t.status === 'active' ? UNUSABLE_ACTIVE : UNUSABLE_ARCHIVED}</p>;
  }
  return (
    <dl className="space-y-1 text-xs">
      {(bindingRows(t) ?? []).map(([label, value]) => (
        <div key={label}>
          <dt className="text-slate-600">{label}</dt>
          <dd className="font-mono break-all">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

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
          <div
            role="region"
            aria-label="Deployment targets list"
            tabIndex={0}
            className="card overflow-x-auto p-0"
          >
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
                    <th scope="col" className={`th ${ACTIONS_CELL}`}>
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
                          {isKnownKind(t.platform_kind) && (
                            <MaturityBadge maturity="experimental" />
                          )}
                        </span>
                      </td>
                      <td className="td min-w-48 font-mono text-xs break-all">{t.address}</td>
                      <td className="td">{t.environment}</td>
                      <td className="td">
                        {t.workspace ?? '-'}
                        {t.project && <> / {t.project}</>}
                      </td>
                      <td className="td min-w-64">
                        <BindingsCell target={t} />
                      </td>
                      <td className="td font-mono text-xs">{t.credential_ref}</td>
                      <td className="td">
                        <StatusBadge status={t.status} />
                      </td>
                      <td className={`td ${ACTIONS_CELL}`}>
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
  const [platform, setPlatform] = useState<PlatformKind | ''>('');
  const [environment, setEnvironment] = useState<DeliveryEnvironment>('dev');
  // The address and bindings typed for each kind; only the chosen kind's values are sent.
  const [values, setValues] = useState<Partial<Record<PlatformKind, Record<string, string>>>>({});
  const [credentialRef, setCredentialRef] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const projects = useProjects(org, workspace || undefined);
  const spec = platform === '' ? null : PLATFORM_KINDS[platform];
  const typed = (platform === '' ? undefined : values[platform]) ?? {};

  function setValue(key: string, value: string) {
    if (platform === '') return;
    setValues((prev) => ({ ...prev, [platform]: { ...prev[platform], [key]: value } }));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (name.trim() === '') next.name = 'Name is required.';
    if (platform === '') next.platform = 'Choose a platform.';
    if (workspace === '') next.workspace = 'Choose a workspace.';
    const trimmed = Object.fromEntries(Object.entries(typed).map(([k, v]) => [k, v.trim()]));
    if (spec) {
      const address = trimmed.address ?? '';
      const addressError =
        address === '' ? `${spec.address.label} is required.` : spec.address.check?.(address);
      if (addressError) next.address = addressError;
      for (const f of spec.fields) {
        if (f.checkbox) continue;
        const value = trimmed[f.key] ?? '';
        if (value === '') {
          if (!f.optional) next[f.key] = `${f.label} is required.`;
        } else {
          const message = f.check?.(value, trimmed);
          if (message) next[f.key] = message;
        }
      }
    }
    if (credentialRef.trim() === '') next.credentialRef = 'Credential reference is required.';
    setErrors(next);
    if (Object.keys(next).length > 0 || platform === '') return;

    const res = await action.run(() =>
      endpoints.registerTarget(org, {
        workspace,
        ...(project ? { project } : {}),
        name: name.trim(),
        ...platformBindings(platform, trimmed),
        address: trimmed.address ?? '',
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
        <div>
          <div className="flex flex-wrap items-end gap-2">
            <SelectField
              label="Platform"
              value={platform}
              disabled={action.busy}
              onChange={(v) => {
                setPlatform(v as PlatformKind | '');
                setErrors({});
                action.reset();
              }}
              options={[
                { value: '', label: 'Select...' },
                ...PLATFORMS.map((value) => ({ value, label: platformLabel(value) })),
              ]}
            />
            {spec && (
              <span className="pb-2">
                <MaturityBadge maturity="experimental" />
              </span>
            )}
          </div>
          {errors.platform && <p className="mt-1 text-sm text-red-700">{errors.platform}</p>}
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
        {spec && (
          <TextField
            key={`${platform}-address`}
            label={spec.address.label}
            value={typed.address ?? ''}
            onChange={(v) => setValue('address', v)}
            error={errors.address}
            maxLength={2048}
            monospace
            help={spec.address.help}
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
        {spec && (
          <div key={platform} className="grid gap-3 sm:grid-cols-2">
            {spec.fields.map((f) =>
              f.checkbox ? (
                <div key={f.key} className="sm:col-span-2">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={typed[f.key] === 'true'}
                      disabled={action.busy}
                      onChange={(e) => setValue(f.key, e.target.checked ? 'true' : '')}
                    />
                    {f.checkbox.label}
                  </label>
                  <p className="mt-1 text-sm text-slate-600">{f.checkbox.help}</p>
                </div>
              ) : (
                <TextField
                  key={f.key}
                  label={f.optional ? `${f.label} (optional)` : f.label}
                  value={typed[f.key] ?? ''}
                  onChange={(v) => setValue(f.key, v)}
                  error={errors[f.key]}
                  maxLength={f.maxLength}
                  monospace
                  help={f.help}
                />
              ),
            )}
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
