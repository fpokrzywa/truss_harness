'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type {
  ApiKeyFilters,
  ApiKeyMeta,
  ApiKeyWithSecret,
  PrincipalType,
} from '@/lib/api/security-types';
import { useCan, usePermissions } from '@/lib/hooks';
import {
  useApiKeys,
  usePermissionCatalog,
  useRefresh,
  useScopeOptions,
  useServiceAccounts,
  useUsers,
} from '@/lib/security-hooks';
import { ConfirmDialog } from '../ConfirmDialog';
import { PermissionChecklist } from '../admin/PermissionChecklist';
import { SecretOnce } from '../admin/SecretOnce';
import {
  ActionError,
  Badge,
  EmptyState,
  formatDate,
  Modal,
  ModalActions,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';

export const MAX_EXPIRY_DAYS = 365; // server default max (SHIFDE_API_KEY_MAX_DAYS); server is authoritative
export const MAX_GRACE_HOURS = 168;

export interface FixedPrincipal {
  type: PrincipalType;
  id: string;
  label: string;
}

interface ApiKeysPanelProps {
  org: string;
  filters?: ApiKeyFilters;
  /** When set, new keys are created for this principal only (service-account detail page). */
  fixedPrincipal?: FixedPrincipal;
  /** Hide the create button (e.g. for a disabled service account). */
  canCreate?: boolean;
}

function keyStatus(k: ApiKeyMeta): { label: string; tone: 'good' | 'bad' | 'warn' } {
  if (k.revoked_at) return { label: 'Revoked', tone: 'bad' };
  if (k.expires_at && new Date(k.expires_at).getTime() < Date.now()) {
    return { label: 'Expired', tone: 'warn' };
  }
  return { label: 'Active', tone: 'good' };
}

/** API key metadata list with create / rotate / revoke. Secrets exist only inside their dialogs. */
export function ApiKeysPanel({
  org,
  filters = {},
  fixedPrincipal,
  canCreate = true,
}: ApiKeysPanelProps) {
  const { can } = useCan(org);
  const keys = useApiKeys(org, filters);
  const accounts = useServiceAccounts(org);
  const users = useUsers(org);
  const refresh = useRefresh(org);
  const revoke = useAction();
  const [creating, setCreating] = useState(false);
  const [rotating, setRotating] = useState<ApiKeyMeta | null>(null);
  const [revoking, setRevoking] = useState<ApiKeyMeta | null>(null);
  const canManage = can('apikey:manage');

  function principalLabel(k: ApiKeyMeta): string {
    if (k.principal_type === 'service_account') {
      return (
        accounts.data?.items.find((a) => a.id === k.principal_id)?.name ??
        `Service account ${k.principal_id}`
      );
    }
    const u = users.data?.items.find((m) => m.user.id === k.principal_id);
    return u ? u.user.display_name : `User ${k.principal_id}`;
  }

  async function confirmRevoke() {
    if (!revoking) return;
    const res = await revoke.run(() => endpoints.revokeApiKey(org, revoking.id));
    setRevoking(null);
    if (res.ok) await refresh('api-keys');
  }

  return (
    <section aria-label="API keys" className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-slate-900">API keys</h2>
        {canManage && canCreate && (
          <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
            Create API key
          </button>
        )}
      </div>
      <ActionError error={revoke.error} title="Could not revoke the API key" />
      <QueryGate query={keys} noun="API keys">
        {(data) => (
          <div className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>
                No API keys yet.{canManage && ' Use "Create API key" to issue the first one.'}
              </EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">API keys</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {[
                      'Name',
                      'Prefix',
                      'Principal',
                      'Scopes',
                      'Expires',
                      'Last used',
                      'Status',
                    ].map((h) => (
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
                  {data.items.map((k) => {
                    const status = keyStatus(k);
                    return (
                      <tr key={k.id}>
                        <td className="td font-medium">{k.name}</td>
                        <td className="td font-mono text-xs">{k.prefix}</td>
                        <td className="td">{principalLabel(k)}</td>
                        <td className="td" title={k.scopes.join(', ')}>
                          {k.scopes.length}
                        </td>
                        <td className="td">{formatDate(k.expires_at)}</td>
                        <td className="td">{formatDate(k.last_used_at)}</td>
                        <td className="td">
                          <Badge tone={status.tone}>{status.label}</Badge>
                        </td>
                        <td className="td">
                          {canManage && !k.revoked_at && (
                            <div className="flex justify-end gap-2">
                              <button type="button" className="btn" onClick={() => setRotating(k)}>
                                Rotate <span className="sr-only">{k.name}</span>
                              </button>
                              <button type="button" className="btn" onClick={() => setRevoking(k)}>
                                Revoke <span className="sr-only">{k.name}</span>
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        )}
      </QueryGate>

      {creating && (
        <CreateApiKeyDialog
          org={org}
          fixedPrincipal={fixedPrincipal}
          onClose={() => setCreating(false)}
        />
      )}
      {rotating && (
        <RotateApiKeyDialog org={org} apiKey={rotating} onClose={() => setRotating(null)} />
      )}
      <ConfirmDialog
        open={revoking !== null}
        title="Revoke API key?"
        confirmLabel="Revoke key"
        busy={revoke.busy}
        onConfirm={() => void confirmRevoke()}
        onCancel={() => setRevoking(null)}
      >
        <p>
          <strong>{revoking?.name}</strong> stops working immediately; requests using it will be
          rejected. This cannot be undone.
        </p>
      </ConfirmDialog>
    </section>
  );
}

function SecretResult({
  result,
  onClose,
  heading,
}: {
  result: ApiKeyWithSecret;
  onClose: () => void;
  heading: string;
}) {
  return (
    <Modal title={heading} onClose={onClose}>
      <p className="mb-3 text-sm text-slate-700">
        Key <strong>{result.api_key.name}</strong> (prefix{' '}
        <code className="font-mono">{result.api_key.prefix}</code>)
        {result.api_key.expires_at && <> expires {formatDate(result.api_key.expires_at)}</>}.
      </p>
      <SecretOnce label="API key secret" value={result.secret} />
      <div className="mt-5 flex justify-end">
        <button type="button" className="btn btn-primary" onClick={onClose}>
          I have copied the secret
        </button>
      </div>
    </Modal>
  );
}

function CreateApiKeyDialog({
  org,
  fixedPrincipal,
  onClose,
}: {
  org: string;
  fixedPrincipal?: FixedPrincipal;
  onClose: () => void;
}) {
  const catalog = usePermissionCatalog();
  const mine = usePermissions(org);
  const accounts = useServiceAccounts(org);
  const users = useUsers(org);
  const { options } = useScopeOptions(org);
  const refresh = useRefresh(org);
  const action = useAction();
  const [name, setName] = useState('');
  const [principalType, setPrincipalType] = useState<PrincipalType>(
    fixedPrincipal?.type ?? 'service_account',
  );
  const [principalId, setPrincipalId] = useState(fixedPrincipal?.id ?? '');
  const [scopes, setScopes] = useState<Set<string>>(new Set());
  const [restricted, setRestricted] = useState<Set<string>>(new Set());
  const [days, setDays] = useState('90');
  const [errors, setErrors] = useState<Record<string, string>>({});
  // The one-time secret lives only here; the parent unmounts this dialog on close.
  const [result, setResult] = useState<ApiKeyWithSecret | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    const d = Number(days);
    if (name.trim() === '') next.name = 'Name is required.';
    if (principalId === '') next.principal = 'Choose who the key acts as.';
    if (scopes.size === 0) next.scopes = 'Select at least one scope.';
    if (!Number.isInteger(d) || d < 1 || d > MAX_EXPIRY_DAYS) {
      next.days = `Enter a whole number of days between 1 and ${MAX_EXPIRY_DAYS}.`;
    }
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const workspace_ids = options
      .filter((o) => o.type === 'workspace' && restricted.has(o.id))
      .map((o) => o.id);
    const project_ids = options
      .filter((o) => o.type === 'project' && restricted.has(o.id))
      .map((o) => o.id);
    const res = await action.run(() =>
      endpoints.createApiKey(org, {
        name: name.trim(),
        principal_type: principalType,
        principal_id: principalId,
        scopes: [...scopes].sort(),
        ...(workspace_ids.length + project_ids.length > 0
          ? {
              resource_scope: {
                ...(workspace_ids.length > 0 ? { workspace_ids } : {}),
                ...(project_ids.length > 0 ? { project_ids } : {}),
              },
            }
          : {}),
        expires_in_days: d,
      }),
    );
    if (res.ok) {
      setResult(res.value);
      await refresh('api-keys');
    }
  }

  if (result) return <SecretResult result={result} onClose={onClose} heading="API key created" />;

  const principalOptions =
    principalType === 'service_account'
      ? (accounts.data?.items ?? [])
          .filter((a) => a.status === 'active')
          .map((a) => ({ value: a.id, label: a.name }))
      : (users.data?.items ?? []).map((m) => ({ value: m.user.id, label: m.user.display_name }));

  return (
    <Modal title="Create API key" wide onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField
          label="Name"
          value={name}
          onChange={setName}
          error={errors.name}
          maxLength={200}
        />
        {fixedPrincipal ? (
          <p className="text-sm text-slate-700">
            Acts as <strong>{fixedPrincipal.label}</strong>
          </p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <SelectField
              label="Principal type"
              value={principalType}
              onChange={(v) => {
                setPrincipalType(v as PrincipalType);
                setPrincipalId('');
              }}
              options={[
                { value: 'service_account', label: 'Service account' },
                { value: 'user', label: 'User' },
              ]}
            />
            <div>
              <SelectField
                label="Principal"
                value={principalId}
                onChange={setPrincipalId}
                options={[{ value: '', label: 'Select...' }, ...principalOptions]}
              />
              {errors.principal && <p className="mt-1 text-sm text-red-700">{errors.principal}</p>}
            </div>
          </div>
        )}
        <div>
          <p className="label">Scopes (a subset of what you can grant)</p>
          {catalog.isPending && (
            <p role="status" className="text-sm text-slate-600">
              Loading permissions...
            </p>
          )}
          {catalog.data && mine.data && (
            <PermissionChecklist
              catalog={catalog.data}
              offered={mine.data}
              held={mine.data}
              selected={scopes}
              onChange={setScopes}
            />
          )}
          {errors.scopes && <p className="mt-1 text-sm text-red-700">{errors.scopes}</p>}
        </div>
        {options.length > 0 && (
          <fieldset>
            <legend className="label">Restrict to workspaces or projects (optional)</legend>
            <div className="max-h-36 space-y-1 overflow-y-auto">
              {options.map((o) => (
                <label key={`${o.type}-${o.id}`} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={restricted.has(o.id)}
                    onChange={(e) =>
                      setRestricted((cur) => {
                        const next = new Set(cur);
                        if (e.target.checked) next.add(o.id);
                        else next.delete(o.id);
                        return next;
                      })
                    }
                  />
                  {o.label}
                  <span className="text-xs text-slate-600">
                    ({o.type === 'workspace' ? 'workspace' : 'project'})
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
        )}
        <TextField
          label="Expires in (days)"
          type="number"
          value={days}
          onChange={setDays}
          min={1}
          max={MAX_EXPIRY_DAYS}
          error={errors.days}
          help={`Required. 1 to ${MAX_EXPIRY_DAYS} days.`}
        />
        <ActionError error={action.error} title="Could not create the API key" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Create key" />
      </form>
    </Modal>
  );
}

function RotateApiKeyDialog({
  org,
  apiKey,
  onClose,
}: {
  org: string;
  apiKey: ApiKeyMeta;
  onClose: () => void;
}) {
  const refresh = useRefresh(org);
  const action = useAction();
  const [grace, setGrace] = useState('24');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ApiKeyWithSecret | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const g = Number(grace);
    if (!Number.isInteger(g) || g < 0 || g > MAX_GRACE_HOURS) {
      setError(`Enter a whole number of hours between 0 and ${MAX_GRACE_HOURS}.`);
      return;
    }
    setError(null);
    const res = await action.run(() => endpoints.rotateApiKey(org, apiKey.id, g));
    if (res.ok) {
      setResult(res.value);
      await refresh('api-keys');
    }
  }

  if (result) return <SecretResult result={result} onClose={onClose} heading="API key rotated" />;

  return (
    <Modal title={`Rotate ${apiKey.name}`} onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <p className="text-sm text-slate-700">
          A new secret is issued. The current key keeps working for the grace period, then expires.
        </p>
        <TextField
          label="Grace period (hours)"
          type="number"
          value={grace}
          onChange={setGrace}
          min={0}
          max={MAX_GRACE_HOURS}
          error={error}
          help={`0 expires the current key immediately. Maximum ${MAX_GRACE_HOURS}.`}
        />
        <ActionError error={action.error} title="Could not rotate the API key" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Rotate key" />
      </form>
    </Modal>
  );
}
