'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { BindingScope, RoleBinding, SubjectType } from '@/lib/api/security-types';
import { useBindings, useRefresh, useRoles, useScopeOptions } from '@/lib/security-hooks';
import { ConfirmDialog } from '../ConfirmDialog';
import { ScopePicker, useOrgId } from './ScopePicker';
import {
  ActionError,
  EmptyState,
  formatDate,
  Modal,
  ModalActions,
  QueryGate,
  SelectField,
  useAction,
} from './ui';

interface RoleBindingsProps {
  org: string;
  subjectType: SubjectType;
  subjectId: string;
  /** Whether the user holds `role:manage`; hides add/remove when false (server still enforces). */
  canManage: boolean;
}

/** Role assignments of one subject, with add (scope picker) and remove-with-confirm. */
export function RoleBindings({ org, subjectType, subjectId, canManage }: RoleBindingsProps) {
  const bindings = useBindings(org, { subject_type: subjectType, subject_id: subjectId });
  const roles = useRoles(org);
  const { options } = useScopeOptions(org);
  const refresh = useRefresh(org);
  const removal = useAction();
  const [adding, setAdding] = useState(false);
  const [target, setTarget] = useState<RoleBinding | null>(null);

  const roleName = (b: RoleBinding) =>
    b.role_name ?? roles.data?.items.find((r) => r.id === b.role_id)?.name ?? b.role_id;

  function scopeLabel(scope: BindingScope): string {
    if (scope.type === 'tenant') return 'Whole organization';
    const name = scope.name ?? options.find((o) => o.id === scope.id)?.label ?? scope.id;
    return `${scope.type === 'workspace' ? 'Workspace' : scope.type === 'project' ? 'Project' : scope.type}: ${name}`;
  }

  async function confirmRemove() {
    if (!target) return;
    const res = await removal.run(() => endpoints.deleteBinding(org, target.id));
    setTarget(null);
    if (res.ok) await refresh('bindings', 'users', 'groups');
  }

  return (
    <section aria-label="Role assignments" className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-base font-semibold text-slate-900">Role assignments</h3>
        {canManage && (
          <button type="button" className="btn" onClick={() => setAdding(true)}>
            Add role assignment
          </button>
        )}
      </div>
      <ActionError error={removal.error} title="Could not remove the role assignment" />
      <QueryGate query={bindings} noun="role assignments">
        {(data) =>
          data.items.length === 0 ? (
            <div className="card p-0">
              <EmptyState>No roles are assigned directly.</EmptyState>
            </div>
          ) : (
            <div className="card p-0">
              <table className="w-full">
                <caption className="sr-only">Role assignments</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    <th scope="col" className="th">
                      Role
                    </th>
                    <th scope="col" className="th">
                      Scope
                    </th>
                    <th scope="col" className="th">
                      Expires
                    </th>
                    <th scope="col" className="th">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((b) => (
                    <tr key={b.id}>
                      <td className="td font-medium">{roleName(b)}</td>
                      <td className="td">{scopeLabel(b.scope)}</td>
                      <td className="td">{formatDate(b.expires_at)}</td>
                      <td className="td text-right">
                        {canManage && (
                          <button type="button" className="btn" onClick={() => setTarget(b)}>
                            Remove <span className="sr-only">{roleName(b)} assignment</span>
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </QueryGate>

      {adding && (
        <AddBindingDialog
          org={org}
          subjectType={subjectType}
          subjectId={subjectId}
          onClose={() => setAdding(false)}
        />
      )}
      <ConfirmDialog
        open={target !== null}
        title="Remove role assignment?"
        confirmLabel="Remove assignment"
        busy={removal.busy}
        onConfirm={() => void confirmRemove()}
        onCancel={() => setTarget(null)}
      >
        <p>
          {target ? `${roleName(target)} (${scopeLabel(target.scope)})` : ''} will no longer apply
          to this subject. This is recorded in the audit log.
        </p>
      </ConfirmDialog>
    </section>
  );
}

function AddBindingDialog({
  org,
  subjectType,
  subjectId,
  onClose,
}: Pick<RoleBindingsProps, 'org' | 'subjectType' | 'subjectId'> & { onClose: () => void }) {
  const roles = useRoles(org);
  const orgId = useOrgId(org) ?? '';
  const refresh = useRefresh(org);
  const action = useAction();
  const [roleId, setRoleId] = useState('');
  const [scope, setScope] = useState<{ type: 'tenant' | 'workspace' | 'project'; id: string }>({
    type: 'tenant',
    id: '',
  });
  const effective = scope.type === 'tenant' ? { ...scope, id: orgId } : scope;
  const valid = roleId !== '' && effective.id !== '';

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    const res = await action.run(() =>
      endpoints.createBinding(org, {
        subject_type: subjectType,
        subject_id: subjectId,
        role_id: roleId,
        scope: { type: effective.type, id: effective.id },
      }),
    );
    if (res.ok) {
      await refresh('bindings', 'users', 'groups');
      onClose();
    }
  }

  return (
    <Modal title="Add role assignment" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4">
        <SelectField
          label="Role"
          value={roleId}
          onChange={setRoleId}
          options={[
            { value: '', label: 'Select a role...' },
            ...(roles.data?.items ?? []).map((r) => ({ value: r.id, label: r.name })),
          ]}
        />
        {roles.isError && (
          <p className="text-sm text-red-700">The role list could not be loaded.</p>
        )}
        <ScopePicker org={org} value={effective} onChange={setScope} />
        <ActionError error={action.error} title="Could not add the role assignment" />
        <ModalActions
          onCancel={onClose}
          busy={action.busy}
          submitLabel="Add assignment"
          submitDisabled={!valid}
        />
      </form>
    </Modal>
  );
}
