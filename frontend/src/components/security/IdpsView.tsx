'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { endpoints, orgPath } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import { PLATFORM_IDP_BASE } from '@/lib/api/security-endpoints';
import type {
  GroupSyncMode,
  Idp,
  IdpInput,
  IdpTestResult,
  JitMode,
} from '@/lib/api/security-types';
import { useCan, useSession } from '@/lib/hooks';
import { useIdps } from '@/lib/security-hooks';
import { NoAccess } from '../NoAccess';
import {
  ActionError,
  Badge,
  EmptyState,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from './useOrg';

/** Tenant identity providers (`idp:manage`). */
export function IdpsView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  return (
    <IdpManager
      title="Identity providers"
      description="OpenID Connect providers users can sign in with for this organization."
      base={orgPath(org)}
      tenant
      canManage={can('idp:manage')}
    />
  );
}

/** Installation-wide identity providers (platform administrators only). */
export function PlatformIdpsView() {
  const session = useSession();
  if (session.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading...
      </p>
    );
  }
  // Server is the authority; non-admins see the same neutral page the API's 403 produces.
  if (!session.data?.user.is_platform_admin) return <NoAccess />;
  return (
    <IdpManager
      title="Platform identity providers"
      description="Installation-wide OpenID Connect providers, available to every organization."
      base={PLATFORM_IDP_BASE}
      canManage
    />
  );
}

function splitScopes(value: string): string[] {
  return [...new Set(value.split(/[\s,]+/).filter(Boolean))];
}

function checkText(c: IdpTestResult['checks'][number]): { text: string; ok: boolean | undefined } {
  if (typeof c === 'string') return { text: c, ok: undefined };
  const label = c.name ?? 'Check';
  const detail = c.detail ?? c.message;
  return { text: detail ? `${label}: ${detail}` : label, ok: c.ok };
}

function IdpManager({
  title,
  description,
  base,
  tenant = false,
  canManage,
}: {
  title: string;
  description: string;
  base: string;
  /** Tenant IdPs never provision just-in-time; only installation IdPs may. */
  tenant?: boolean;
  canManage: boolean;
}) {
  const idps = useIdps(base);
  const qc = useQueryClient();
  const tester = useAction();
  const [editing, setEditing] = useState<Idp | 'new' | null>(null);
  const [testing, setTesting] = useState<{ idp: Idp; result: IdpTestResult } | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);

  async function test(idp: Idp) {
    setTestingId(idp.id);
    const res = await tester.run(() => endpoints.testIdp(base, idp.id));
    setTestingId(null);
    if (res.ok) setTesting({ idp, result: res.value });
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title={title}
        description={description}
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>
              Add identity provider
            </button>
          )
        }
      />
      <ActionError error={tester.error} title="Could not test the connection" />
      <QueryGate query={idps} noun="identity providers">
        {(data) => (
          <section aria-label="Identity providers" className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>No identity providers are configured.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Identity providers</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    <th scope="col" className="th">
                      Name
                    </th>
                    <th scope="col" className="th">
                      Issuer
                    </th>
                    <th scope="col" className="th">
                      Client secret
                    </th>
                    <th scope="col" className="th">
                      Status
                    </th>
                    <th scope="col" className="th">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((i) => (
                    <tr key={i.id}>
                      <td className="td font-medium">{i.display_name}</td>
                      <td className="td break-all text-xs">{i.issuer}</td>
                      <td className="td">
                        <Badge tone={i.client_secret_set ? 'good' : 'warn'}>
                          {i.client_secret_set ? 'Set' : 'Not set'}
                        </Badge>
                      </td>
                      <td className="td">
                        <Badge tone={i.status === 'active' ? 'good' : 'neutral'}>
                          {i.status === 'active' ? 'Active' : 'Disabled'}
                        </Badge>
                      </td>
                      <td className="td">
                        <div className="flex justify-end gap-2">
                          {canManage && (
                            <>
                              <button
                                type="button"
                                className="btn"
                                disabled={testingId === i.id}
                                onClick={() => void test(i)}
                              >
                                {testingId === i.id ? 'Testing...' : 'Test connection'}{' '}
                                <span className="sr-only">for {i.display_name}</span>
                              </button>
                              <button type="button" className="btn" onClick={() => setEditing(i)}>
                                Edit <span className="sr-only">{i.display_name}</span>
                              </button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}
      </QueryGate>

      {editing !== null && (
        <IdpDialog
          base={base}
          tenant={tenant}
          idp={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => qc.invalidateQueries({ queryKey: ['idps', base] })}
        />
      )}
      {testing && (
        <Modal title={`Test result: ${testing.idp.display_name}`} onClose={() => setTesting(null)}>
          <p
            role="status"
            className={`rounded-md border p-3 text-sm ${
              testing.result.ok
                ? 'border-green-300 bg-green-50 text-green-900'
                : 'border-red-300 bg-red-50 text-red-900'
            }`}
          >
            {testing.result.ok ? 'Connection test passed.' : 'Connection test failed.'}
          </p>
          <ul className="mt-3 space-y-1 text-sm">
            {testing.result.checks.map((c, idx) => {
              const { text, ok } = checkText(c);
              return (
                <li key={`${text}-${idx}`}>
                  {ok === undefined ? '' : ok ? 'Passed: ' : 'Failed: '}
                  {text}
                </li>
              );
            })}
          </ul>
          <div className="mt-5 flex justify-end">
            <button type="button" className="btn" onClick={() => setTesting(null)}>
              Close
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function IdpDialog({
  base,
  tenant,
  idp,
  onClose,
  onSaved,
}: {
  base: string;
  tenant: boolean;
  idp: Idp | null;
  onClose: () => void;
  onSaved: () => unknown;
}) {
  const action = useAction();
  const [displayName, setDisplayName] = useState(idp?.display_name ?? '');
  const [issuer, setIssuer] = useState(idp?.issuer ?? '');
  const [discovery, setDiscovery] = useState(idp?.discovery_url ?? '');
  const [clientId, setClientId] = useState(idp?.client_id ?? '');
  // Write-only: never pre-filled from the server and cleared from state once submitted.
  const [secret, setSecret] = useState('');
  const [scopes, setScopes] = useState((idp?.scopes ?? ['openid', 'email', 'profile']).join(' '));
  const [emailClaim, setEmailClaim] = useState(idp?.claim_mappings.email ?? 'email');
  const [groupsClaim, setGroupsClaim] = useState(idp?.claim_mappings.groups ?? 'groups');
  const [jit, setJit] = useState<JitMode>(tenant ? 'off' : (idp?.jit ?? 'off'));
  const [groupSync, setGroupSync] = useState<GroupSyncMode>(idp?.group_sync ?? 'off');
  const [status, setStatus] = useState<'active' | 'disabled'>(idp?.status ?? 'active');
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (displayName.trim() === '') next.displayName = 'Display name is required.';
    if (issuer.trim() === '') next.issuer = 'Issuer is required.';
    if (discovery.trim() === '') next.discovery = 'Discovery URL is required.';
    if (clientId.trim() === '') next.clientId = 'Client ID is required.';
    if (idp === null && secret === '') next.secret = 'Client secret is required.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const input: IdpInput = {
      display_name: displayName.trim(),
      issuer: issuer.trim(),
      discovery_url: discovery.trim(),
      client_id: clientId.trim(),
      ...(secret !== '' ? { client_secret: secret } : {}),
      scopes: splitScopes(scopes),
      claim_mappings: {
        email: emailClaim.trim() || 'email',
        groups: groupsClaim.trim() || 'groups',
      },
      jit: tenant ? 'off' : jit,
      group_sync: groupSync,
      status,
      ...(idp ? { row_version: idp.row_version } : {}),
    };
    setSecret('');
    const res = await action.run(() =>
      idp ? endpoints.updateIdp(base, idp.id, input) : endpoints.createIdp(base, input),
    );
    if (res.ok) {
      await onSaved();
      onClose();
    }
  }

  return (
    <Modal
      title={idp ? `Edit ${idp.display_name}` : 'Add identity provider'}
      wide
      onClose={onClose}
      busy={action.busy}
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField
          label="Display name"
          value={displayName}
          onChange={setDisplayName}
          error={errors.displayName}
        />
        <TextField
          label="Issuer"
          value={issuer}
          onChange={setIssuer}
          error={
            errors.issuer ??
            (action.error instanceof ApiError && action.error.code === 'issuer_conflict'
              ? 'This issuer is already registered to another identity provider'
              : null)
          }
        />
        <TextField
          label="Discovery URL"
          value={discovery}
          onChange={setDiscovery}
          error={errors.discovery}
          help="Usually the issuer followed by /.well-known/openid-configuration. HTTPS only."
        />
        <TextField
          label="Client ID"
          value={clientId}
          onChange={setClientId}
          error={errors.clientId}
        />
        <div>
          <TextField
            label="Client secret"
            type="password"
            value={secret}
            onChange={setSecret}
            autoComplete="new-password"
            error={errors.secret}
            placeholder={idp?.client_secret_set ? 'Leave blank to keep the current secret' : ''}
            help="Write-only: it is stored encrypted and never shown again."
          />
          <p className="mt-1 text-sm text-slate-700">
            Current status:{' '}
            <strong>{idp ? (idp.client_secret_set ? 'set' : 'not set') : 'not set'}</strong>
          </p>
        </div>
        <TextField
          label="Scopes"
          value={scopes}
          onChange={setScopes}
          help="Separate with spaces."
        />
        <div className="grid gap-3 sm:grid-cols-2">
          <TextField label="Email claim" value={emailClaim} onChange={setEmailClaim} />
          <TextField label="Groups claim" value={groupsClaim} onChange={setGroupsClaim} />
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <SelectField
              label="Just-in-time provisioning"
              value={tenant ? 'off' : jit}
              disabled={tenant}
              onChange={(v) => setJit(v as JitMode)}
              options={[
                { value: 'off', label: 'Off' },
                { value: 'create_user', label: 'Create user' },
                { value: 'create_and_join', label: 'Create user and join' },
              ]}
            />
            {tenant && (
              <p className="mt-1 text-xs text-slate-600">
                Just-in-time provisioning is only available for installation identity providers.
              </p>
            )}
          </div>
          <SelectField
            label="Group sync"
            value={groupSync}
            onChange={(v) => setGroupSync(v as GroupSyncMode)}
            options={[
              { value: 'off', label: 'Off' },
              { value: 'sync', label: 'Sync groups' },
            ]}
          />
          <SelectField
            label="Status"
            value={status}
            onChange={(v) => setStatus(v as 'active' | 'disabled')}
            options={[
              { value: 'active', label: 'Active' },
              { value: 'disabled', label: 'Disabled' },
            ]}
          />
        </div>
        {!(action.error instanceof ApiError && action.error.code === 'issuer_conflict') && (
          <ActionError error={action.error} title="Could not save the identity provider" />
        )}
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Save" />
      </form>
    </Modal>
  );
}
