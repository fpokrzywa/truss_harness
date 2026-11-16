'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { PermissionDef, Role } from '@/lib/api/security-types';
import { useCan, usePermissions } from '@/lib/hooks';
import { usePermissionCatalog, useRefresh, useRoles } from '@/lib/security-hooks';
import { PermissionChecklist } from '../admin/PermissionChecklist';
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

const ROLE_KEY_RE = /^[a-z0-9][a-z0-9_.-]{0,62}$/;

export function RolesView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const roles = useRoles(org);
  const catalog = usePermissionCatalog();
  const [editing, setEditing] = useState<Role | 'new' | null>(null);
  const canManage = can('role:manage');

  return (
    <div className="space-y-6">
      <PageHeader
        title="Roles"
        description="System roles are read-only templates. Custom roles combine permissions from the catalog."
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>
              Create custom role
            </button>
          )
        }
      />
      <QueryGate query={roles} noun="roles">
        {(data) => (
          <>
            <section aria-label="Roles" className="card p-0">
              {data.items.length === 0 ? (
                <EmptyState>No roles found.</EmptyState>
              ) : (
                <table className="w-full">
                  <caption className="sr-only">Roles</caption>
                  <thead className="border-b border-slate-200">
                    <tr>
                      <th scope="col" className="th">
                        Role
                      </th>
                      <th scope="col" className="th">
                        Type
                      </th>
                      <th scope="col" className="th">
                        Permissions
                      </th>
                      <th scope="col" className="th">
                        <span className="sr-only">Actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {data.items.map((r) => (
                      <tr key={r.id}>
                        <td className="td">
                          <span className="font-medium">{r.name}</span>
                          <p className="font-mono text-xs text-slate-600">{r.key}</p>
                        </td>
                        <td className="td">
                          <Badge>{r.is_system ? 'System template' : 'Custom'}</Badge>
                        </td>
                        <td className="td">{r.permissions.length}</td>
                        <td className="td text-right">
                          <button type="button" className="btn" onClick={() => setEditing(r)}>
                            {r.is_system || !canManage ? 'View' : 'Edit'}{' '}
                            <span className="sr-only">{r.name}</span>
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
            {catalog.data && data.items.length > 0 && (
              <PermissionMatrix roles={data.items} catalog={catalog.data} />
            )}
          </>
        )}
      </QueryGate>

      {editing !== null && (
        <RoleEditor
          org={org}
          role={editing === 'new' ? null : editing}
          canManage={canManage}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}

function PermissionMatrix({ roles, catalog }: { roles: Role[]; catalog: PermissionDef[] }) {
  const sorted = [...catalog].sort((a, b) => a.key.localeCompare(b.key));
  return (
    <details className="card p-0">
      <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-slate-900">
        Permission matrix
      </summary>
      <div className="overflow-x-auto">
        <table className="w-full">
          <caption className="sr-only">Permissions granted by each role</caption>
          <thead className="border-y border-slate-200">
            <tr>
              <th scope="col" className="th">
                Permission
              </th>
              {roles.map((r) => (
                <th key={r.id} scope="col" className="th">
                  {r.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {sorted.map((p) => (
              <tr key={p.key}>
                <th scope="row" className="td text-left font-mono text-xs font-normal">
                  {p.key}
                </th>
                {roles.map((r) => (
                  <td key={r.id} className="td">
                    {r.permissions.includes(p.key) ? (
                      <span>
                        Yes
                        <span className="sr-only">
                          {' '}
                          ({r.name} grants {p.key})
                        </span>
                      </span>
                    ) : (
                      <span className="text-slate-400">
                        -<span className="sr-only">No</span>
                      </span>
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function RoleEditor({
  org,
  role,
  canManage,
  onClose,
}: {
  org: string;
  role: Role | null;
  canManage: boolean;
  onClose: () => void;
}) {
  const catalog = usePermissionCatalog();
  const mine = usePermissions(org);
  const refresh = useRefresh(org);
  const action = useAction();
  const readOnly = !canManage || (role?.is_system ?? false);
  const [key, setKey] = useState(role?.key ?? '');
  const [name, setName] = useState(role?.name ?? '');
  const [description, setDescription] = useState(role?.description ?? '');
  const [selected, setSelected] = useState<Set<string>>(() => new Set(role?.permissions ?? []));
  const [errors, setErrors] = useState<{ key?: string; name?: string }>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next = {
      key:
        role === null && !ROLE_KEY_RE.test(key)
          ? 'Use lowercase letters, digits, "-", "_" or ".".'
          : undefined,
      name: name.trim() === '' ? 'Name is required.' : undefined,
    };
    setErrors(next);
    if (next.key || next.name) return;
    const permissions = [...selected].sort();
    const base = {
      name: name.trim(),
      ...(description.trim() !== '' ? { description: description.trim() } : {}),
      permissions,
    };
    const res = await action.run(() =>
      role === null
        ? endpoints.createRole(org, { key, ...base })
        : endpoints.updateRole(org, role.id, { ...base, row_version: role.row_version }),
    );
    if (res.ok) {
      await refresh('roles');
      onClose();
    }
  }

  const title = role === null ? 'Create custom role' : readOnly ? role.name : `Edit ${role.name}`;
  return (
    <Modal title={title} wide onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        {role?.is_system && (
          <p className="text-sm text-slate-600">
            System roles are templates and cannot be changed.
          </p>
        )}
        {role === null && (
          <TextField
            label="Key"
            value={key}
            onChange={setKey}
            error={errors.key}
            monospace
            help="Stable identifier, e.g. release-manager. Cannot be changed later."
          />
        )}
        <TextField
          label="Name"
          value={name}
          onChange={setName}
          error={errors.name}
          disabled={readOnly}
          maxLength={200}
        />
        <TextField
          label="Description (optional)"
          value={description}
          onChange={setDescription}
          multiline
          disabled={readOnly}
        />
        <div>
          <p className="label">Permissions ({selected.size} selected)</p>
          {catalog.isPending && (
            <p role="status" className="text-sm text-slate-600">
              Loading permissions...
            </p>
          )}
          {catalog.isError && (
            <ActionError error={catalog.error} title="Could not load the permission catalog" />
          )}
          {catalog.data && (
            <PermissionChecklist
              catalog={catalog.data}
              selected={selected}
              onChange={setSelected}
              held={mine.data}
              disabled={readOnly}
            />
          )}
        </div>
        <ActionError error={action.error} title="Could not save the role" />
        {readOnly ? (
          <div className="flex justify-end">
            <button type="button" className="btn" onClick={onClose}>
              Close
            </button>
          </div>
        ) : (
          <ModalActions
            onCancel={onClose}
            busy={action.busy}
            submitLabel={role === null ? 'Create role' : 'Save role'}
          />
        )}
      </form>
    </Modal>
  );
}
