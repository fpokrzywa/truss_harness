'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { ArtifactKind, ArtifactScope, ArtifactVisibility } from '@/lib/api/build-types';
import { useArtifacts, useRefreshBuild } from '@/lib/build-hooks';
import { useCan } from '@/lib/hooks';
import { useScopeOptions } from '@/lib/security-hooks';
import { ScopePicker, useOrgId } from '../admin/ScopePicker';
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
import { useOrgParam } from '../security/useOrg';
import { artifactHref, KIND_INFO, latestLabel, ScopeLabel } from './shared';

export function ArtifactListView({ kind }: { kind: ArtifactKind }) {
  const org = useOrgParam();
  const info = KIND_INFO[kind];
  const { can } = useCan(org);
  const { options } = useScopeOptions(org);
  const orgId = useOrgId(org) ?? '';
  const [status, setStatus] = useState('');
  const [q, setQ] = useState('');
  const [scopeKey, setScopeKey] = useState('');
  const [creating, setCreating] = useState(false);

  const scopeFilter = scopeKey
    ? scopeKey === 'tenant'
      ? { scope_type: 'tenant', scope_id: orgId }
      : {
          scope_type: options.find((o) => o.id === scopeKey)?.type,
          scope_id: scopeKey,
        }
    : {};
  const query = useArtifacts(org, kind, {
    ...scopeFilter,
    status: status || undefined,
    q: q || undefined,
  });

  return (
    <div className="space-y-6">
      <PageHeader
        title={info.label}
        description={info.blurb}
        actions={
          can(`${info.perm}:create`) && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              Create {info.singular}
            </button>
          )
        }
      />
      <div className="grid gap-3 sm:grid-cols-3">
        <TextField label="Search" value={q} onChange={setQ} placeholder="Name or key" />
        <SelectField
          label="Status"
          value={status}
          onChange={setStatus}
          options={[
            { value: '', label: 'Any status' },
            { value: 'active', label: 'Active' },
            { value: 'disabled', label: 'Disabled' },
            { value: 'retired', label: 'Retired' },
          ]}
        />
        <SelectField
          label="Scope"
          value={scopeKey}
          onChange={setScopeKey}
          options={[
            { value: '', label: 'All visible scopes' },
            { value: 'tenant', label: 'Organization' },
            ...options.map((o) => ({ value: o.id, label: o.label })),
          ]}
        />
      </div>
      <QueryGate query={query} noun={info.label.toLowerCase()}>
        {(data) => (
          <section aria-label={info.label} className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>No {info.label.toLowerCase()} match.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">{info.label}</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Name', 'Scope', 'Visibility', 'Status', 'Latest published'].map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((a) => (
                    <tr key={a.id}>
                      <td className="td font-medium">
                        <Link
                          className="text-blue-800 underline"
                          href={artifactHref(org, kind, a.id)}
                        >
                          {a.name}
                        </Link>
                        <div className="font-mono text-xs text-slate-600">{a.key}</div>
                      </td>
                      <td className="td">
                        <ScopeLabel org={org} scope={a.scope} />
                      </td>
                      <td className="td">{a.visibility}</td>
                      <td className="td">
                        <Badge tone={a.status === 'active' ? 'good' : 'neutral'}>{a.status}</Badge>
                      </td>
                      <td className="td">
                        {latestLabel(a)}
                        {a.has_draft && (
                          <span className="ml-2">
                            <Badge tone="warn">Draft</Badge>
                          </span>
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
      {creating && <CreateDialog org={org} kind={kind} onClose={() => setCreating(false)} />}
    </div>
  );
}

function CreateDialog({
  org,
  kind,
  onClose,
}: {
  org: string;
  kind: ArtifactKind;
  onClose: () => void;
}) {
  const info = KIND_INFO[kind];
  const router = useRouter();
  const refresh = useRefreshBuild(org);
  const orgId = useOrgId(org) ?? '';
  const [key, setKey] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [scope, setScope] = useState<ArtifactScope | null>(null);
  const [visibility, setVisibility] = useState<ArtifactVisibility>('scope');
  const { busy, error, run } = useAction();
  const effectiveScope: ArtifactScope = scope ?? { type: 'tenant', id: orgId };
  const keyValid = /^[a-z0-9][a-z0-9._-]*$/.test(key);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await run(() =>
      endpoints.createArtifact(org, kind, {
        key,
        name: name.trim(),
        ...(description.trim() ? { description: description.trim() } : {}),
        scope: effectiveScope,
        visibility,
      }),
    );
    if (r.ok) {
      await refresh();
      router.push(artifactHref(org, kind, r.value.id));
    }
  }

  return (
    <Modal title={`Create ${info.singular}`} onClose={onClose} busy={busy}>
      <form onSubmit={submit} className="space-y-3">
        <TextField
          label="Key"
          value={key}
          onChange={setKey}
          required
          monospace
          help="Lowercase letters, digits, dot, dash or underscore. Unique within its scope."
          error={key && !keyValid ? 'Use lowercase letters, digits, ".", "-" or "_".' : null}
        />
        <TextField label="Name" value={name} onChange={setName} required />
        <TextField label="Description" value={description} onChange={setDescription} multiline />
        <ScopePicker org={org} value={effectiveScope} onChange={setScope} />
        <SelectField
          label="Visibility"
          value={visibility}
          onChange={(v) => setVisibility(v as ArtifactVisibility)}
          options={[
            { value: 'private', label: 'Private (owner only)' },
            { value: 'scope', label: 'Scope (everyone in the scope)' },
            { value: 'tenant', label: 'Organization' },
          ]}
        />
        <ActionError error={error} title={`Could not create ${info.singular}`} />
        <ModalActions
          onCancel={onClose}
          submitLabel="Create"
          busy={busy}
          submitDisabled={!keyValid || !name.trim() || !effectiveScope.id}
        />
      </form>
    </Modal>
  );
}
