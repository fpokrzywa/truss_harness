'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { CreateUserResult, UserMember } from '@/lib/api/security-types';
import { useCan } from '@/lib/hooks';
import { useRefresh, useRoles, useUsers } from '@/lib/security-hooks';
import { validateEmail, validateName } from '@/lib/validation';
import { ConfirmDialog } from '../ConfirmDialog';
import { RoleBindings } from '../admin/RoleBindings';
import { SecretOnce } from '../admin/SecretOnce';
import {
  ActionError,
  Badge,
  EmptyState,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from './useOrg';

export function UsersView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const users = useUsers(org);
  const refresh = useRefresh(org);
  const statusAction = useAction();
  const [adding, setAdding] = useState(false);
  const [rolesFor, setRolesFor] = useState<UserMember | null>(null);
  const [toggle, setToggle] = useState<UserMember | null>(null);

  const canManage = can('member:manage');

  async function confirmToggle() {
    if (!toggle) return;
    const next = toggle.status === 'active' ? 'suspended' : 'active';
    const res = await statusAction.run(() =>
      endpoints.setUserStatus(
        org,
        toggle.user.id,
        next,
        toggle.row_version ?? toggle.user.row_version ?? 0,
      ),
    );
    setToggle(null);
    if (res.ok) await refresh('users');
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Users"
        description="Members of this organization, their groups and direct role assignments."
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}>
              Add user
            </button>
          )
        }
      />
      <ActionError error={statusAction.error} title="Could not change the user status" />
      <QueryGate query={users} noun="users">
        {(data) => (
          <section aria-label="Users" className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>
                No users yet.{canManage && ' Use "Add user" to invite the first one.'}
              </EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Users</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    <th scope="col" className="th">
                      Name
                    </th>
                    <th scope="col" className="th">
                      Status
                    </th>
                    <th scope="col" className="th">
                      Groups
                    </th>
                    <th scope="col" className="th">
                      Direct roles
                    </th>
                    <th scope="col" className="th">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((m) => (
                    <tr key={m.user.id}>
                      <td className="td">
                        <span className="font-medium">{m.user.display_name}</span>
                        <p className="text-xs text-slate-600">{m.user.email}</p>
                      </td>
                      <td className="td">
                        <Badge tone={m.status === 'active' ? 'good' : 'warn'}>
                          {m.status === 'active' ? 'Active' : 'Suspended'}
                        </Badge>
                      </td>
                      <td className="td">
                        {(m.groups ?? []).map((g) => g.name).join(', ') || '-'}
                      </td>
                      <td className="td">
                        {(m.role_bindings ?? []).map((b) => b.role_name ?? b.role_id).join(', ') ||
                          '-'}
                      </td>
                      <td className="td">
                        <div className="flex justify-end gap-2">
                          {can('role:read') && (
                            <button type="button" className="btn" onClick={() => setRolesFor(m)}>
                              Roles <span className="sr-only">for {m.user.display_name}</span>
                            </button>
                          )}
                          {canManage && (
                            <button type="button" className="btn" onClick={() => setToggle(m)}>
                              {m.status === 'active' ? 'Suspend' : 'Reactivate'}{' '}
                              <span className="sr-only">{m.user.display_name}</span>
                            </button>
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

      {adding && <AddUserDialog org={org} onClose={() => setAdding(false)} />}
      {rolesFor && (
        <Modal
          title={`Roles for ${rolesFor.user.display_name}`}
          wide
          onClose={() => setRolesFor(null)}
        >
          <RoleBindings
            org={org}
            subjectType="user"
            subjectId={rolesFor.user.id}
            canManage={can('role:manage')}
          />
          <div className="mt-4 flex justify-end">
            <button type="button" className="btn" onClick={() => setRolesFor(null)}>
              Close
            </button>
          </div>
        </Modal>
      )}
      <ConfirmDialog
        open={toggle !== null}
        title={toggle?.status === 'active' ? 'Suspend user?' : 'Reactivate user?'}
        confirmLabel={toggle?.status === 'active' ? 'Suspend user' : 'Reactivate user'}
        busy={statusAction.busy}
        onConfirm={() => void confirmToggle()}
        onCancel={() => setToggle(null)}
      >
        {toggle?.status === 'active' ? (
          <p>
            <strong>{toggle.user.display_name}</strong> will be signed out and their sessions and
            API keys in this organization are revoked immediately.
          </p>
        ) : (
          <p>
            <strong>{toggle?.user.display_name}</strong> will be able to sign in again. Revoked API
            keys are not restored.
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}

function AddUserDialog({ org, onClose }: { org: string; onClose: () => void }) {
  const roles = useRoles(org);
  const refresh = useRefresh(org);
  const action = useAction();
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [roleIds, setRoleIds] = useState<string[]>([]);
  const [errors, setErrors] = useState<{ email?: string; name?: string }>({});
  // Lives only in this dialog's state; unmounting on close discards the one-time token.
  const [result, setResult] = useState<CreateUserResult | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next = {
      email: validateEmail(email) ?? undefined,
      name: validateName(name) ?? undefined,
    };
    setErrors(next);
    if (next.email || next.name) return;
    const res = await action.run(() =>
      endpoints.createUser(org, {
        email: email.trim(),
        display_name: name.trim(),
        ...(roleIds.length > 0 ? { role_ids: roleIds } : {}),
      }),
    );
    if (res.ok) {
      setResult(res.value);
      await refresh('users');
    }
  }

  if (result) {
    return (
      <Modal title="User added" onClose={onClose}>
        <p className="text-sm text-slate-700">
          <strong>{result.user.display_name}</strong> ({result.user.email}) is now a member.
        </p>
        <div className="mt-3">
          {result.setup_token ? (
            <SecretOnce label="Setup token" value={result.setup_token} />
          ) : (
            <p className="text-sm text-slate-700">
              No setup token was issued: this user already has a credential.
            </p>
          )}
        </div>
        <div className="mt-5 flex justify-end">
          <button type="button" className="btn btn-primary" onClick={onClose}>
            Done
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Add user" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField
          label="Email"
          type="email"
          value={email}
          onChange={setEmail}
          error={errors.email}
          autoComplete="off"
        />
        <TextField
          label="Display name"
          value={name}
          onChange={setName}
          error={errors.name}
          maxLength={200}
        />
        {roles.data && roles.data.items.length > 0 && (
          <fieldset>
            <legend className="label">Initial roles (optional, organization-wide)</legend>
            <div className="max-h-40 space-y-1 overflow-y-auto">
              {roles.data.items.map((r) => (
                <label key={r.id} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={roleIds.includes(r.id)}
                    onChange={(e) =>
                      setRoleIds((cur) =>
                        e.target.checked ? [...cur, r.id] : cur.filter((id) => id !== r.id),
                      )
                    }
                  />
                  {r.name}
                </label>
              ))}
            </div>
          </fieldset>
        )}
        <ActionError error={action.error} title="Could not add the user" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Add user" />
      </form>
    </Modal>
  );
}
