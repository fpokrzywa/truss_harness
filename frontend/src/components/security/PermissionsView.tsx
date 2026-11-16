'use client';

import { useMemo, useState } from 'react';
import { usePermissionCatalog } from '@/lib/security-hooks';
import { groupByResource } from '../admin/PermissionChecklist';
import { Badge, EmptyState, PageHeader, QueryGate, TextField } from '../admin/ui';

export function PermissionsView() {
  const catalog = usePermissionCatalog();
  const [filter, setFilter] = useState('');
  const q = filter.trim().toLowerCase();
  const groups = useMemo(
    () =>
      groupByResource(
        (catalog.data ?? []).filter(
          (p) =>
            q === '' ||
            p.key.toLowerCase().includes(q) ||
            (p.description ?? '').toLowerCase().includes(q),
        ),
      ),
    [catalog.data, q],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Permissions"
        description="The permission catalog. Privileged permissions can only be granted by principals that hold them."
      />
      <QueryGate query={catalog} noun="permissions">
        {() => (
          <>
            <div className="max-w-sm">
              <TextField label="Filter permissions" value={filter} onChange={setFilter} />
            </div>
            {groups.length === 0 ? (
              <div className="card p-0">
                <EmptyState>No permissions match.</EmptyState>
              </div>
            ) : (
              groups.map(([type, perms]) => (
                <section key={type} aria-label={type} className="card p-0">
                  <h2 className="border-b border-slate-200 px-3 py-2 text-sm font-semibold text-slate-900">
                    {type}
                  </h2>
                  <table className="w-full">
                    <caption className="sr-only">{type} permissions</caption>
                    <thead className="border-b border-slate-200">
                      <tr>
                        <th scope="col" className="th">
                          Permission
                        </th>
                        <th scope="col" className="th">
                          Description
                        </th>
                        <th scope="col" className="th">
                          Flags
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {perms.map((p) => (
                        <tr key={p.key}>
                          <td className="td font-mono text-xs">{p.key}</td>
                          <td className="td">{p.description ?? '-'}</td>
                          <td className="td">
                            {p.is_privileged ? <Badge tone="warn">Privileged</Badge> : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </section>
              ))
            )}
          </>
        )}
      </QueryGate>
    </div>
  );
}
