'use client';

import Link from 'next/link';
import { usePermissions } from '@/lib/hooks';
import { visibleNav } from '@/lib/nav';
import { parseScope } from '@/lib/scope';
import { usePathname } from 'next/navigation';
import { PageHeader, QueryGate } from '../admin/ui';
import { useOrgParam } from './useOrg';

/** Landing page: only the Security areas the user's permissions allow (FR-UI-004). */
export function SecurityHome() {
  const org = useOrgParam();
  const pathname = usePathname();
  const permissions = usePermissions(org);

  return (
    <div className="space-y-6">
      <PageHeader title="Security" description="Identity, access and credential administration." />
      <QueryGate query={permissions} noun="your permissions">
        {(perms) => {
          const areas = visibleNav(parseScope(pathname), {
            isPlatformAdmin: false,
            permissions: perms,
          }).filter((i) => i.group === 'Security');
          return areas.length === 0 ? (
            <p className="text-sm text-slate-600">
              Your roles do not include any security administration permissions.
            </p>
          ) : (
            <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {areas.map((a) => (
                <li key={a.id} className="card">
                  <Link href={a.href} className="font-medium text-blue-800 underline">
                    {a.label}
                  </Link>
                </li>
              ))}
            </ul>
          );
        }}
      </QueryGate>
    </div>
  );
}
