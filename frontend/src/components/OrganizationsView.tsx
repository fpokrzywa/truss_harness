'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { Org, OrgStatus } from '@/lib/api/types';
import { keys, useOrgs, useSession } from '@/lib/hooks';
import { orgHref } from '@/lib/scope';
import { CreateScopeForm } from './CreateScopeForm';
import { NoAccess } from './NoAccess';
import { QueryError } from './QueryError';

export function OrganizationsView() {
  const queryClient = useQueryClient();
  const session = useSession();
  const isAdmin = session.data?.user.is_platform_admin === true;
  const list = useOrgs(true); // GET /orgs?scope=all (A1); 403 renders the no-access view
  const [created, setCreated] = useState<Org | null>(null);

  const create = useMutation({
    mutationFn: (v: { slug: string; name: string; admin_email?: string }) => endpoints.createOrg(v),
    onSuccess: async (org) => {
      setCreated(org);
      // Memberships live in the session; refresh both so selectors pick up the new org.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['orgs'] }),
        queryClient.invalidateQueries({ queryKey: keys.session }),
      ]);
    },
  });

  if (session.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading...
      </p>
    );
  }
  // Server is the authority; non-admins see the same neutral page the API's 403 produces.
  if (!isAdmin) return <NoAccess />;
  if (list.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading organizations...
      </p>
    );
  }
  if (list.isError) return <QueryError error={list.error} />;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900">Organizations</h1>

      {created && (
        <p
          role="status"
          className="rounded-md border border-green-300 bg-green-50 p-3 text-sm text-green-900"
        >
          Organization <strong>{created.name}</strong> created.{' '}
          <Link href={orgHref(created.slug)} className="font-medium underline">
            Open it
          </Link>
        </p>
      )}

      <section aria-label="Organizations" className="card p-0">
        {list.data.items.length === 0 ? (
          <p className="p-4 text-sm text-slate-600">
            No organizations are visible to you yet. Create one below.
          </p>
        ) : (
          <OrganizationsTable orgs={list.data.items} />
        )}
      </section>

      <CreateScopeForm
        noun="organization"
        withDescription={false}
        withAdminEmail
        onSubmit={(v) => create.mutateAsync(v)}
      />
    </div>
  );
}

const STATUS_LABEL: Record<OrgStatus, string> = {
  active: 'Active',
  suspended: 'Suspended',
  archived: 'Archived',
};

/** The organizations table, without hooks so it can be rendered in tests. */
export function OrganizationsTable({
  orgs,
}: {
  orgs: Pick<Org, 'id' | 'slug' | 'name' | 'status'>[];
}) {
  return (
    <table className="w-full">
      <caption className="sr-only">Organizations</caption>
      <thead className="border-b border-slate-200">
        <tr>
          <th scope="col" className="th">
            Name
          </th>
          <th scope="col" className="th">
            Slug
          </th>
          <th scope="col" className="th">
            Status
          </th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {orgs.map((o) => (
          <tr key={o.id}>
            <td className="td">
              <Link href={orgHref(o.slug)} className="font-medium text-blue-800 underline">
                {o.name}
              </Link>
            </td>
            <td className="td font-mono text-xs">{o.slug}</td>
            <td className="td">{STATUS_LABEL[o.status]}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
