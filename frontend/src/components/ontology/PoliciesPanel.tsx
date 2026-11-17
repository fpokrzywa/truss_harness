'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { Artifact } from '@/lib/api/build-types';
import type { OntologySpec, Policy, PolicyInput, PolicySubject } from '@/lib/api/ontology-types';
import { checkPolicyFilter } from '@/lib/ontology-spec';
import { useOntologySpec, usePolicies, useRefreshOntology } from '@/lib/ontology-hooks';
import { useGroups, useRoles, useUsers } from '@/lib/security-hooks';
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
import { ConfirmDialog } from '../ConfirmDialog';
import { PrincipalAttributesDialog } from './PrincipalAttributesDialog';

/** Access policies (default deny; contract section 3). Requires `ontology:manage_policies`. */
export function PoliciesPanel({ org, artifact }: { org: string; artifact: Artifact }) {
  const policies = usePolicies(org, artifact.id);
  const { spec, pending } = useOntologySpec(org, artifact);
  const refresh = useRefreshOntology(org, artifact.id);
  const [editing, setEditing] = useState<Policy | 'new' | null>(null);
  const [deleting, setDeleting] = useState<Policy | null>(null);
  const [attrs, setAttrs] = useState(false);
  const del = useAction();
  const roles = useRoles(org);
  const groups = useGroups(org);
  const users = useUsers(org);

  const subjectName = (s: PolicySubject): string => {
    const found =
      s.type === 'role'
        ? roles.data?.items.find((r) => r.id === s.id)?.name
        : s.type === 'group'
          ? groups.data?.items.find((g) => g.id === s.id)?.name
          : users.data?.items.find((u) => u.user.id === s.id)?.user.display_name;
    return `${s.type}: ${found ?? s.id}`;
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title="Access policies"
        description="Default deny: a principal sees an object type only if a policy allows it. All properties covers every non-restricted property; a restricted property is only readable through a policy that names it explicitly. Row filters are compiled into source queries as parameters and may read principal attributes (for example region), which are set per user or service account under Principal attributes."
        actions={
          <div className="flex gap-2">
            <button type="button" className="btn" onClick={() => setAttrs(true)}>
              Principal attributes
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={pending}
              onClick={() => setEditing('new')}
            >
              Add policy
            </button>
          </div>
        }
      />
      <QueryGate query={policies} noun="policies">
        {(data) => (
          <section aria-label="Policies" className="card p-0">
            {data.length === 0 ? (
              <EmptyState>No policies. Nobody can query this ontology yet.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Policies</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Object type', 'Subject', 'Properties', 'Row filter', ''].map((h, i) => (
                      <th key={i} scope="col" className="th">
                        {h || <span className="sr-only">Actions</span>}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.map((p) => (
                    <tr key={p.id}>
                      <td className="td font-mono text-xs">{p.object_type_key}</td>
                      <td className="td">{subjectName(p.subject)}</td>
                      <td className="td text-xs">
                        {p.property_keys === null
                          ? 'All non-restricted properties'
                          : p.property_keys.join(', ')}
                      </td>
                      <td className="td font-mono text-xs">{p.row_filter ?? ''}</td>
                      <td className="td">
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            className="btn"
                            aria-label={`Edit policy for ${p.object_type_key} ${subjectName(p.subject)}`}
                            onClick={() => setEditing(p)}
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            className="btn"
                            aria-label={`Delete policy for ${p.object_type_key} ${subjectName(p.subject)}`}
                            onClick={() => setDeleting(p)}
                          >
                            Delete
                          </button>
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
      {attrs && <PrincipalAttributesDialog org={org} onClose={() => setAttrs(false)} />}
      {editing !== null && (
        <PolicyDialog
          org={org}
          ontologyId={artifact.id}
          spec={spec}
          policy={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={refresh}
        />
      )}
      <ConfirmDialog
        open={!!deleting}
        title="Delete this policy?"
        confirmLabel="Delete"
        busy={del.busy}
        onCancel={() => setDeleting(null)}
        onConfirm={() => {
          if (!deleting) return;
          void del
            .run(() => endpoints.deletePolicy(org, artifact.id, deleting.id))
            .then(async (r) => {
              if (r.ok) {
                setDeleting(null);
                await refresh();
              }
            });
        }}
      >
        <p>Principals covered only by this policy lose access to the object type.</p>
        <ActionError error={del.error} title="Could not delete policy" />
      </ConfirmDialog>
    </div>
  );
}

function PolicyDialog({
  org,
  ontologyId,
  spec,
  policy,
  onClose,
  onSaved,
}: {
  org: string;
  ontologyId: string;
  spec: OntologySpec;
  policy: Policy | null;
  onClose: () => void;
  onSaved: () => unknown;
}) {
  const action = useAction();
  const roles = useRoles(org);
  const groups = useGroups(org);
  const users = useUsers(org);
  const [objectType, setObjectType] = useState(
    policy?.object_type_key ?? spec.object_types[0]?.key ?? '',
  );
  const [subjectType, setSubjectType] = useState<PolicySubject['type']>(
    policy?.subject.type ?? 'role',
  );
  const [subjectId, setSubjectId] = useState(policy?.subject.id ?? '');
  const [all, setAll] = useState(policy ? policy.property_keys === null : true);
  const [props, setProps] = useState<string[]>(policy?.property_keys ?? []);
  const [filter, setFilter] = useState(policy?.row_filter ?? '');

  const ot = spec.object_types.find((o) => o.key === objectType);
  const restricted = (ot?.properties ?? []).filter((p) => p.classification === 'restricted');
  // "All properties" (null) never includes restricted ones on the server: they need an explicit name.
  const chosenRestricted = restricted
    .filter((p) => !all && props.includes(p.key))
    .map((p) => p.key);
  const omittedRestricted = all ? restricted.map((p) => p.key) : [];
  const filterCheck = filter.trim() ? checkPolicyFilter(filter) : null;

  const source =
    subjectType === 'role'
      ? {
          query: roles,
          opts: (roles.data?.items ?? []).map((r) => ({ value: r.id, label: r.name })),
        }
      : subjectType === 'group'
        ? {
            query: groups,
            opts: (groups.data?.items ?? []).map((g) => ({ value: g.id, label: g.name })),
          }
        : {
            query: users,
            opts: (users.data?.items ?? []).map((u) => ({
              value: u.user.id,
              label: `${u.user.display_name} (${u.user.email})`,
            })),
          };
  const listed = source.query.isSuccess;
  const valid =
    !!objectType &&
    !!subjectId &&
    (all || props.length > 0) &&
    (filterCheck === null || filterCheck.ok);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    const input: PolicyInput = {
      object_type_key: objectType,
      property_keys: all ? null : props,
      subject: { type: subjectType, id: subjectId },
      effect: 'allow',
      ...(filter.trim() ? { row_filter: filter.trim() } : {}),
    };
    const r = await action.run(() =>
      policy
        ? endpoints.updatePolicy(org, ontologyId, policy, input)
        : endpoints.createPolicy(org, ontologyId, input),
    );
    if (r.ok) {
      await onSaved();
      onClose();
    }
  }

  return (
    <Modal title={policy ? 'Edit policy' : 'Add policy'} onClose={onClose} busy={action.busy} wide>
      <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
        <SelectField
          label="Object type"
          value={objectType}
          onChange={(v) => {
            setObjectType(v);
            setProps([]);
          }}
          options={[
            { value: '', label: 'Select...' },
            ...spec.object_types.map((o) => ({ value: o.key, label: `${o.label} (${o.key})` })),
          ]}
        />
        <div className="grid gap-3 sm:grid-cols-2">
          <SelectField
            label="Subject type"
            value={subjectType}
            onChange={(v) => {
              setSubjectType(v as PolicySubject['type']);
              setSubjectId('');
            }}
            options={[
              { value: 'role', label: 'Role' },
              { value: 'group', label: 'Group' },
              { value: 'user', label: 'User' },
            ]}
          />
          {listed ? (
            <SelectField
              label="Subject"
              value={subjectId}
              onChange={setSubjectId}
              options={[{ value: '', label: 'Select...' }, ...source.opts]}
            />
          ) : (
            <TextField
              label="Subject id"
              value={subjectId}
              onChange={setSubjectId}
              monospace
              help={
                source.query.isPending
                  ? 'Loading subjects...'
                  : 'Could not list subjects; enter the id directly.'
              }
            />
          )}
        </div>

        <fieldset>
          <legend className="text-sm font-semibold">Properties</legend>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
            All non-restricted properties
          </label>
          {!all && (
            <div className="mt-1 flex flex-wrap gap-3">
              {(ot?.properties ?? []).map((p) => (
                <label key={p.key} className="flex items-center gap-1 text-sm">
                  <input
                    type="checkbox"
                    checked={props.includes(p.key)}
                    onChange={(e) =>
                      setProps((cur) =>
                        e.target.checked ? [...cur, p.key] : cur.filter((k) => k !== p.key),
                      )
                    }
                  />
                  {p.key}
                  {p.classification === 'restricted' && <Badge tone="bad">restricted</Badge>}
                </label>
              ))}
            </div>
          )}
          {omittedRestricted.length > 0 && (
            <p className="mt-2 text-sm text-slate-700">
              Restricted properties ({omittedRestricted.join(', ')}) are not covered by this choice.
              Untick it and name them explicitly to grant them.
            </p>
          )}
          {chosenRestricted.length > 0 && (
            <p role="alert" className="mt-2 text-sm text-amber-900">
              This policy grants access to restricted properties ({chosenRestricted.join(', ')}).
              Grant them only to subjects that are cleared for sensitive data.
            </p>
          )}
        </fieldset>

        <TextField
          label="Row filter"
          value={filter}
          onChange={setFilter}
          monospace
          help='Optional safe expression, for example row.region == principal.attributes.region. Fields must start with "row." or "principal.attributes.".'
          error={filterCheck && !filterCheck.ok ? filterCheck.error : null}
        />
        <ActionError error={action.error} title="Could not save policy" />
        <ModalActions
          onCancel={onClose}
          submitLabel="Save policy"
          busy={action.busy}
          submitDisabled={!valid}
        />
      </form>
    </Modal>
  );
}
