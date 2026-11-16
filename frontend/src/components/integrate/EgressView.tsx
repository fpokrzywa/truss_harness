'use client';

import { useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { EgressPolicy, EgressRules } from '@/lib/api/integrate-types';
import { useCan } from '@/lib/hooks';
import { useEgressPolicies, useRefreshInt } from '@/lib/integrate-hooks';
import {
  ActionError,
  EmptyState,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from '../security/useOrg';

interface RulesForm {
  schemes: string;
  ports: string;
  hosts: string;
  cidrs: string;
  allowPrivate: boolean;
  allowRedirects: boolean;
  maxBytes: string;
  timeout: string;
}

const lines = (s: string) =>
  s
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);

function toForm(r: EgressRules | undefined): RulesForm {
  return {
    schemes: (r?.allowed_schemes ?? []).join('\n'),
    ports: (r?.allowed_ports ?? []).join('\n'),
    hosts: (r?.allowed_hosts ?? []).join('\n'),
    cidrs: (r?.allowed_cidrs ?? []).join('\n'),
    allowPrivate: r?.allow_private === true,
    allowRedirects: r?.allow_redirects === true,
    maxBytes: r?.max_response_bytes !== undefined ? String(r.max_response_bytes) : '',
    timeout: r?.timeout_seconds !== undefined ? String(r.timeout_seconds) : '',
  };
}

function validateForm(f: RulesForm): Record<string, string> {
  const e: Record<string, string> = {};
  const bad = lines(f.ports).find((p) => !/^\d+$/.test(p) || Number(p) < 1 || Number(p) > 65535);
  if (bad !== undefined) e.ports = `"${bad}" is not a valid port (1-65535).`;
  if (f.maxBytes.trim() !== '' && !(Number.isInteger(Number(f.maxBytes)) && Number(f.maxBytes) > 0))
    e.maxBytes = 'Enter a positive whole number.';
  if (f.timeout.trim() !== '' && !(Number(f.timeout) > 0)) e.timeout = 'Enter a positive number.';
  return e;
}

function toRules(f: RulesForm): EgressRules {
  return {
    allowed_schemes: lines(f.schemes),
    allowed_ports: lines(f.ports).map(Number),
    allowed_hosts: lines(f.hosts),
    allowed_cidrs: lines(f.cidrs),
    allow_private: f.allowPrivate,
    allow_redirects: f.allowRedirects,
    ...(f.maxBytes.trim() ? { max_response_bytes: Number(f.maxBytes) } : {}),
    ...(f.timeout.trim() ? { timeout_seconds: Number(f.timeout) } : {}),
  };
}

function RulesFields({
  form,
  setForm,
  errors,
}: {
  form: RulesForm;
  setForm: (f: RulesForm) => void;
  errors: Record<string, string>;
}) {
  const set = <K extends keyof RulesForm>(k: K, v: RulesForm[K]) => setForm({ ...form, [k]: v });
  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-600">
        Private, loopback, link-local and metadata addresses are denied unless explicitly allowed.
        Effective policy is the installation default narrowed by the organization and the
        connection.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField
          label="Allowed schemes"
          value={form.schemes}
          onChange={(v) => set('schemes', v)}
          multiline
          help="One per line, for example https."
        />
        <TextField
          label="Allowed ports"
          value={form.ports}
          onChange={(v) => set('ports', v)}
          multiline
          error={errors.ports}
          help="One per line."
        />
        <TextField
          label="Allowed hosts"
          value={form.hosts}
          onChange={(v) => set('hosts', v)}
          multiline
          help="One per line."
        />
        <TextField
          label="Allowed CIDR ranges"
          value={form.cidrs}
          onChange={(v) => set('cidrs', v)}
          multiline
          help="One per line."
        />
        <TextField
          label="Max response bytes"
          type="number"
          value={form.maxBytes}
          onChange={(v) => set('maxBytes', v)}
          error={errors.maxBytes}
        />
        <TextField
          label="Timeout (seconds)"
          type="number"
          value={form.timeout}
          onChange={(v) => set('timeout', v)}
          error={errors.timeout}
        />
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={form.allowPrivate}
          onChange={(e) => set('allowPrivate', e.target.checked)}
        />
        Allow private network addresses
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={form.allowRedirects}
          onChange={(e) => set('allowRedirects', e.target.checked)}
        />
        Follow redirects
      </label>
    </div>
  );
}

export function EgressView() {
  const org = useOrgParam();
  const { can, loaded } = useCan(org);
  const canManage = can('egress:manage');
  const policies = useEgressPolicies(org, canManage);
  const [editing, setEditing] = useState<EgressPolicy | 'new' | null>(null);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Egress policies"
        description="Control which outbound destinations connections may reach. Applies in addition to the installation default."
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>
              Create policy
            </button>
          )
        }
      />
      {loaded && !canManage ? (
        <p role="note" className="text-sm text-slate-700">
          You do not have permission to manage egress policies.
        </p>
      ) : (
        <QueryGate query={policies} noun="egress policies">
          {(data) => (
            <section aria-label="Egress policies" className="card p-0">
              {data.items.length === 0 ? (
                <EmptyState>
                  No organization egress policies. The installation default applies.
                </EmptyState>
              ) : (
                <table className="w-full">
                  <caption className="sr-only">Egress policies</caption>
                  <thead className="border-b border-slate-200">
                    <tr>
                      {['Name', 'Schemes', 'Hosts', 'Private addresses'].map((h) => (
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
                    {data.items.map((p) => (
                      <tr key={p.id}>
                        <td className="td font-medium">{p.name}</td>
                        <td className="td">{(p.allowed_schemes ?? []).join(', ') || '-'}</td>
                        <td className="td">
                          {(p.allowed_hosts ?? []).join(', ') || 'Any permitted'}
                        </td>
                        <td className="td">{p.allow_private ? 'Allowed' : 'Denied'}</td>
                        <td className="td text-right">
                          <button type="button" className="btn" onClick={() => setEditing(p)}>
                            Edit <span className="sr-only">{p.name}</span>
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          )}
        </QueryGate>
      )}
      {editing && (
        <PolicyDialog
          org={org}
          policy={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}

function PolicyDialog({
  org,
  policy,
  onClose,
}: {
  org: string;
  policy: EgressPolicy | null;
  onClose: () => void;
}) {
  const refresh = useRefreshInt(org);
  const action = useAction();
  const [name, setName] = useState(policy?.name ?? '');
  const [description, setDescription] = useState(policy?.description ?? '');
  const [form, setForm] = useState(toForm(policy ?? undefined));
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next = validateForm(form);
    if (name.trim() === '') next.name = 'Name is required.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    const body = {
      name: name.trim(),
      ...(description.trim() ? { description: description.trim() } : {}),
      ...toRules(form),
    };
    const res = await action.run(() =>
      policy
        ? endpoints.updateEgressPolicy(org, policy.id, { ...body, row_version: policy.row_version })
        : endpoints.createEgressPolicy(org, body),
    );
    if (res.ok) {
      await refresh('egress');
      onClose();
    }
  }

  return (
    <Modal
      title={policy ? `Edit ${policy.name}` : 'Create egress policy'}
      onClose={onClose}
      busy={action.busy}
      wide
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField label="Name" value={name} onChange={setName} error={errors.name} />
        <TextField label="Description" value={description} onChange={setDescription} />
        <RulesFields form={form} setForm={setForm} errors={errors} />
        <ActionError error={action.error} title="Could not save the policy" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Save policy" />
      </form>
    </Modal>
  );
}

/** Installation-wide default egress policy (platform admins). */
export function PlatformEgressView() {
  const current = useQuery({
    queryKey: ['int', '_platform', 'egress'],
    queryFn: () => endpoints.getEgressDefault(),
  });
  return (
    <div className="space-y-6">
      <PageHeader
        title="Default egress policy"
        description="The installation-wide default. Organization and connection policies can only narrow it. Production defaults deny private addresses."
      />
      <QueryGate query={current} noun="the default egress policy">
        {(data) => <DefaultForm initial={data} />}
      </QueryGate>
    </div>
  );
}

function DefaultForm({ initial }: { initial: EgressRules }) {
  const action = useAction();
  const [form, setForm] = useState(toForm(initial));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    const next = validateForm(form);
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    const res = await action.run(() => endpoints.putEgressDefault(toRules(form)));
    if (res.ok) setSaved(true);
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="card space-y-4" noValidate>
      <RulesFields form={form} setForm={setForm} errors={errors} />
      <ActionError error={action.error} title="Could not save the default policy" />
      {saved && (
        <p role="status" className="text-sm text-green-800">
          Default policy saved.
        </p>
      )}
      <button type="submit" className="btn btn-primary" disabled={action.busy}>
        {action.busy ? 'Saving...' : 'Save default policy'}
      </button>
    </form>
  );
}
