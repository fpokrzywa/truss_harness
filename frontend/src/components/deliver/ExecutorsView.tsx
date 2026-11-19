'use client';

import { Fragment, useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type {
  CredentialIssued,
  CredentialOut,
  DeliveryExecutorOut,
} from '@/lib/api/delivery-types';
import { useExecutorCredentials, useExecutors, useRefreshDelivery } from '@/lib/delivery-hooks';
import { useCan } from '@/lib/hooks';
import { ConfirmDialog } from '../ConfirmDialog';
import { SecretOnce } from '../admin/SecretOnce';
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
import { StatusBadge } from '../integrate/Maturity';
import { useOrgParam } from '../security/useOrg';

const COLUMNS = ['Name', 'ID', 'Status', 'Last seen', 'Version', 'Created'];

const CREDENTIAL_TONE = { live: 'good', revoked: 'bad', expired: 'warn' } as const;

/** Delivery executors and their credentials. Secrets exist only inside their result dialogs. */
export function ExecutorsView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const executors = useExecutors(org);
  const refresh = useRefreshDelivery(org);
  const disable = useAction();
  const [creating, setCreating] = useState(false);
  const [disabling, setDisabling] = useState<DeliveryExecutorOut | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const canManage = can('delivery:executor:manage');

  async function confirmDisable() {
    if (!disabling) return;
    const res = await disable.run(() => endpoints.setExecutorStatus(org, disabling.id, 'disabled'));
    setDisabling(null);
    if (res.ok) await refresh('executors', 'credentials');
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Executors"
        description="Executors run approved deployments inside your network. At most one executor can be active per organization."
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              Create executor
            </button>
          )
        }
      />
      <ActionError error={disable.error} title="Could not disable the executor" />
      <QueryGate query={executors} noun="executors">
        {(data) => (
          <div className="card overflow-x-auto p-0">
            {data.items.length === 0 ? (
              <EmptyState>
                No executors yet.{canManage && ' Use "Create executor" to add one.'}
              </EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Executors</caption>
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
                  {data.items.map((x) => {
                    const open = expanded === x.id;
                    const panelId = `credentials-${x.id}`;
                    return (
                      <Fragment key={x.id}>
                        <tr>
                          <td className="td font-medium">{x.name}</td>
                          <td className="td font-mono text-xs">{x.id}</td>
                          <td className="td">
                            <StatusBadge status={x.status} />
                          </td>
                          <td className="td">{formatDate(x.last_seen_at)}</td>
                          <td className="td">{x.version ?? '-'}</td>
                          <td className="td">{formatDate(x.created_at)}</td>
                          <td className="td">
                            <div className="flex justify-end gap-2">
                              <button
                                type="button"
                                className="btn"
                                aria-expanded={open}
                                aria-controls={open ? panelId : undefined}
                                onClick={() => setExpanded(open ? null : x.id)}
                              >
                                Credentials <span className="sr-only">{x.name}</span>
                              </button>
                              {canManage && x.status === 'active' && (
                                <button
                                  type="button"
                                  className="btn"
                                  onClick={() => setDisabling(x)}
                                >
                                  Disable <span className="sr-only">{x.name}</span>
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                        {open && (
                          <tr id={panelId}>
                            <td className="td bg-slate-50" colSpan={COLUMNS.length + 1}>
                              <CredentialsPanel org={org} executor={x} canManage={canManage} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        )}
      </QueryGate>

      {creating && <CreateExecutorDialog org={org} onClose={() => setCreating(false)} />}
      <ConfirmDialog
        open={disabling !== null}
        title="Disable executor?"
        confirmLabel="Disable executor"
        busy={disable.busy}
        onConfirm={() => void confirmDisable()}
        onCancel={() => setDisabling(null)}
      >
        <p>
          <strong>{disabling?.name}</strong> stops taking work, and all of its credentials are
          revoked immediately. It cannot be re-enabled from this screen.
        </p>
      </ConfirmDialog>
    </div>
  );
}

function CredentialsPanel({
  org,
  executor,
  canManage,
}: {
  org: string;
  executor: DeliveryExecutorOut;
  canManage: boolean;
}) {
  const credentials = useExecutorCredentials(org, executor.id);
  const refresh = useRefreshDelivery(org);
  const revoke = useAction();
  // Issue (credential null) or rotate (the live credential): the dialog owns any secret it gets.
  const [issuing, setIssuing] = useState<{ rotate: CredentialOut | null } | null>(null);
  const [revoking, setRevoking] = useState<CredentialOut | null>(null);

  async function confirmRevoke() {
    if (!revoking) return;
    const res = await revoke.run(() =>
      endpoints.revokeExecutorCredential(org, executor.id, revoking.id),
    );
    setRevoking(null);
    if (res.ok) await refresh('credentials');
  }

  return (
    <section aria-label={`Credentials of ${executor.name}`} className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-900">Credentials</h2>
        {canManage && executor.status === 'active' && (
          <button type="button" className="btn" onClick={() => setIssuing({ rotate: null })}>
            Issue credential
          </button>
        )}
      </div>
      <ActionError error={revoke.error} title="Could not revoke the credential" />
      <QueryGate query={credentials} noun="credentials">
        {(items) =>
          items.length === 0 ? (
            <EmptyState>No credentials issued.</EmptyState>
          ) : (
            <table className="w-full">
              <caption className="sr-only">Credentials of {executor.name}</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {['Prefix', 'Status', 'Created', 'Expires', 'Revoked'].map((h) => (
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
                {items.map((c) => (
                  <tr key={c.id}>
                    <td className="td font-mono text-xs">{c.prefix}</td>
                    <td className="td">
                      <Badge tone={CREDENTIAL_TONE[c.status] ?? 'neutral'}>{c.status}</Badge>
                    </td>
                    <td className="td">{formatDate(c.created_at)}</td>
                    <td className="td">{formatDate(c.expires_at)}</td>
                    <td className="td">{formatDate(c.revoked_at)}</td>
                    <td className="td">
                      {canManage && c.status === 'live' && (
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            className="btn"
                            onClick={() => setIssuing({ rotate: c })}
                          >
                            Rotate <span className="sr-only">{c.prefix}</span>
                          </button>
                          <button type="button" className="btn" onClick={() => setRevoking(c)}>
                            Revoke <span className="sr-only">{c.prefix}</span>
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        }
      </QueryGate>

      {issuing && (
        <IssueCredentialDialog
          org={org}
          executor={executor}
          rotate={issuing.rotate}
          onClose={() => setIssuing(null)}
        />
      )}
      <ConfirmDialog
        open={revoking !== null}
        title="Revoke credential?"
        confirmLabel="Revoke credential"
        busy={revoke.busy}
        onConfirm={() => void confirmRevoke()}
        onCancel={() => setRevoking(null)}
      >
        <p>
          Credential <code className="font-mono">{revoking?.prefix}</code> stops working
          immediately; the executor can no longer sign in with it. This cannot be undone.
        </p>
      </ConfirmDialog>
    </section>
  );
}

function IssueCredentialDialog({
  org,
  executor,
  rotate,
  onClose,
}: {
  org: string;
  executor: DeliveryExecutorOut;
  rotate: CredentialOut | null;
  onClose: () => void;
}) {
  const refresh = useRefreshDelivery(org);
  const action = useAction();
  // The one-time secret lives only here; the parent unmounts this dialog on close.
  const [result, setResult] = useState<CredentialIssued | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const res = await action.run(() =>
      rotate
        ? endpoints.rotateExecutorCredential(org, executor.id, rotate.id)
        : endpoints.issueExecutorCredential(org, executor.id),
    );
    if (res.ok) {
      setResult(res.value);
      await refresh('credentials');
    }
  }

  if (result) {
    return (
      <Modal title={rotate ? 'Credential rotated' : 'Credential issued'} onClose={onClose}>
        <p className="mb-3 text-sm text-slate-700">
          New credential for <strong>{executor.name}</strong> (prefix{' '}
          <code className="font-mono">{result.credential.prefix}</code>) expires{' '}
          {formatDate(result.credential.expires_at)}.
        </p>
        <SecretOnce label="Executor credential" value={result.secret} />
        <div className="mt-5 flex justify-end">
          <button type="button" className="btn btn-primary" onClick={onClose}>
            I have copied the credential
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      title={rotate ? 'Rotate credential' : `Issue credential for ${executor.name}`}
      onClose={onClose}
      busy={action.busy}
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <p className="text-sm text-slate-700">
          {rotate ? (
            <>
              A new credential is issued. Credential{' '}
              <code className="font-mono">{rotate.prefix}</code> keeps working until you revoke it.
            </>
          ) : (
            'A new credential is issued for this executor. An executor can have at most two live credentials.'
          )}
        </p>
        <ActionError
          error={action.error}
          title={rotate ? 'Could not rotate the credential' : 'Could not issue the credential'}
        />
        <ModalActions
          onCancel={onClose}
          busy={action.busy}
          submitLabel={rotate ? 'Rotate credential' : 'Issue credential'}
        />
      </form>
    </Modal>
  );
}

function CreateExecutorDialog({ org, onClose }: { org: string; onClose: () => void }) {
  const refresh = useRefreshDelivery(org);
  const action = useAction();
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (name.trim() === '') {
      setError('Name is required.');
      return;
    }
    setError(null);
    const res = await action.run(() => endpoints.createExecutor(org, name.trim()));
    if (res.ok) {
      await refresh('executors');
      onClose();
    }
  }

  return (
    <Modal title="Create executor" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField label="Name" value={name} onChange={setName} error={error} maxLength={200} />
        <ActionError error={action.error} title="Could not create the executor" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Create executor" />
      </form>
    </Modal>
  );
}
