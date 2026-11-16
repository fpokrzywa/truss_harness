'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { ScopeType, SecretMeta, SecretScope } from '@/lib/api/security-types';
import { useCan } from '@/lib/hooks';
import { useRefresh, useSecrets } from '@/lib/security-hooks';
import { ConfirmDialog } from '../ConfirmDialog';
import { ScopePicker, useOrgId } from '../admin/ScopePicker';
import {
  ActionError,
  Badge,
  EmptyState,
  formatDate,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from './useOrg';

function scopeText(scope: SecretScope | undefined): string {
  if (!scope) return 'Organization';
  if (typeof scope === 'string') return scope;
  return `${scope.type}${scope.name ? `: ${scope.name}` : ''}`;
}

export function SecretsView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const secrets = useSecrets(org);
  const refresh = useRefresh(org);
  const disable = useAction();
  const [creating, setCreating] = useState(false);
  const [rotating, setRotating] = useState<SecretMeta | null>(null);
  const [disabling, setDisabling] = useState<SecretMeta | null>(null);
  const canManage = can('secret:manage');

  async function confirmDisable() {
    if (!disabling) return;
    const res = await disable.run(() => endpoints.disableSecret(org, disabling));
    setDisabling(null);
    if (res.ok) await refresh('secrets');
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Secrets"
        description="Metadata only. Secret values are write-only: they are never displayed and cannot be read back."
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              Create secret
            </button>
          )
        }
      />
      <ActionError error={disable.error} title="Could not disable the secret" />
      <QueryGate query={secrets} noun="secrets">
        {(data) => (
          <section aria-label="Secrets" className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>No secrets yet.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Secrets</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Name', 'Scope', 'Kind', 'Version', 'Rotated', 'Last used', 'Status'].map(
                      (h) => (
                        <th key={h} scope="col" className="th">
                          {h}
                        </th>
                      ),
                    )}
                    <th scope="col" className="th">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((s) => (
                    <tr key={s.id}>
                      <td className="td font-medium">{s.name}</td>
                      <td className="td">{scopeText(s.scope)}</td>
                      <td className="td">{s.kind ?? '-'}</td>
                      <td className="td">{s.version}</td>
                      <td className="td">{formatDate(s.rotated_at ?? s.created_at)}</td>
                      <td className="td">{formatDate(s.last_used_at)}</td>
                      <td className="td">
                        <Badge tone={s.status === 'disabled' ? 'neutral' : 'good'}>
                          {s.status === 'disabled' ? 'Disabled' : 'Active'}
                        </Badge>
                      </td>
                      <td className="td">
                        {canManage && s.status !== 'disabled' && (
                          <div className="flex justify-end gap-2">
                            <button type="button" className="btn" onClick={() => setRotating(s)}>
                              Rotate <span className="sr-only">{s.name}</span>
                            </button>
                            <button type="button" className="btn" onClick={() => setDisabling(s)}>
                              Disable <span className="sr-only">{s.name}</span>
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}
      </QueryGate>

      {creating && <CreateSecretDialog org={org} onClose={() => setCreating(false)} />}
      {rotating && (
        <RotateSecretDialog org={org} secret={rotating} onClose={() => setRotating(null)} />
      )}
      <ConfirmDialog
        open={disabling !== null}
        title="Disable secret?"
        confirmLabel="Disable secret"
        busy={disable.busy}
        onConfirm={() => void confirmDisable()}
        onCancel={() => setDisabling(null)}
      >
        <p>
          <strong>{disabling?.name}</strong> can no longer be resolved by connections or tools that
          reference it.
        </p>
      </ConfirmDialog>
    </div>
  );
}

/** Password-type, autocomplete-off input for a secret value. The caller clears it on submit. */
function ValueField({
  value,
  onChange,
  error,
}: {
  value: string;
  onChange: (v: string) => void;
  error?: string;
}) {
  return (
    <TextField
      label="Secret value"
      type="password"
      value={value}
      onChange={onChange}
      autoComplete="new-password"
      error={error}
      help="Write-only. It is encrypted on the server and never shown again."
    />
  );
}

function CreateSecretDialog({ org, onClose }: { org: string; onClose: () => void }) {
  const refresh = useRefresh(org);
  const orgId = useOrgId(org) ?? '';
  const action = useAction();
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [scope, setScope] = useState<{ type: ScopeType; id: string }>({ type: 'tenant', id: '' });
  const [errors, setErrors] = useState<{ name?: string; value?: string; scope?: string }>({});
  const effective = scope.type === 'tenant' ? { ...scope, id: orgId } : scope;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next = {
      name: name.trim() === '' ? 'Name is required.' : undefined,
      value: value === '' ? 'A value is required.' : undefined,
      scope: effective.id === '' ? 'Choose a scope.' : undefined,
    };
    setErrors(next);
    if (next.name || next.value || next.scope) return;
    const payload = {
      name: name.trim(),
      value,
      // Organization-wide secrets omit `scope`; narrower ones carry {type, id}.
      ...(effective.type !== 'tenant' ? { scope: { type: effective.type, id: effective.id } } : {}),
    };
    setValue(''); // the value is never kept in UI state after submit
    const res = await action.run(() => endpoints.createSecret(org, payload));
    if (res.ok) {
      await refresh('secrets');
      onClose();
    }
  }

  return (
    <Modal title="Create secret" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField
          label="Name"
          value={name}
          onChange={setName}
          error={errors.name}
          maxLength={200}
        />
        <ValueField value={value} onChange={setValue} error={errors.value} />
        <ScopePicker org={org} value={effective} onChange={setScope} />
        {errors.scope && <p className="text-sm text-red-700">{errors.scope}</p>}
        <ActionError error={action.error} title="Could not create the secret" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Create secret" />
      </form>
    </Modal>
  );
}

function RotateSecretDialog({
  org,
  secret,
  onClose,
}: {
  org: string;
  secret: SecretMeta;
  onClose: () => void;
}) {
  const refresh = useRefresh(org);
  const action = useAction();
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | undefined>();

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (value === '') {
      setError('A new value is required.');
      return;
    }
    setError(undefined);
    const next = value;
    setValue(''); // never retained after submit
    const res = await action.run(() => endpoints.rotateSecret(org, secret.id, next));
    if (res.ok) {
      await refresh('secrets');
      onClose();
    }
  }

  return (
    <Modal title={`Rotate ${secret.name}`} onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <p className="text-sm text-slate-700">
          Currently version {secret.version}. Submitting creates version {secret.version + 1}.
        </p>
        <ValueField value={value} onChange={setValue} error={error} />
        <ActionError error={action.error} title="Could not rotate the secret" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Rotate secret" />
      </form>
    </Modal>
  );
}
