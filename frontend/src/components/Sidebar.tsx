'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useState } from 'react';
import { usePermissions, useSession } from '@/lib/hooks';
import { currentNavGroup, visibleNav } from '@/lib/nav';
import { parseScope } from '@/lib/scope';
import { NavGroupIcon } from './NavGroupIcon';

/** Shown when the path belongs to no section, such as a page outside the navigation. */
const FALLBACK_GROUP = 'Home';

export function Sidebar({
  isPlatformAdmin,
  collapsed = false,
  onExpand,
}: {
  isPlatformAdmin: boolean;
  /** Show only section icons, for the closed rail. */
  collapsed?: boolean;
  onExpand?: () => void;
}) {
  const pathname = usePathname();
  const urlScope = parseScope(pathname);
  const session = useSession();

  // Platform pages have no organization in the URL. Keep offering the one the user was last in, or
  // their first active one, so the organization sections do not vanish on those pages.
  const [lastOrg, setLastOrg] = useState(urlScope.org);
  if (urlScope.org && urlScope.org !== lastOrg) setLastOrg(urlScope.org);
  const firstOrg = session.data?.memberships.find((m) => m.org.status === 'active')?.org.slug;
  const scope = urlScope.org ? urlScope : { ...urlScope, org: lastOrg ?? firstOrg };

  // Effective permissions at the current scope drive the Security group (FR-UI-004). While
  // loading or on error no permission-gated area is shown; the server stays authoritative.
  const permissions = usePermissions(scope.org, scope.ws, scope.proj);
  const items = visibleNav(scope, {
    isPlatformAdmin,
    permissions: permissions.data,
    features: session.data?.features,
  });
  const groups = [...new Set(items.map((i) => i.group))];
  const current = currentNavGroup(items, pathname);

  // One section open at a time. A click holds only on the page it was made on: navigating opens
  // the new page's section. `group: null` means the user closed the open section.
  const [choice, setChoice] = useState<{ path: string; group: string | null } | null>(null);
  const openGroup = choice?.path === pathname ? choice.group : (current ?? FALLBACK_GROUP);

  const toggle = (group: string) =>
    setChoice({ path: pathname, group: group === openGroup ? null : group });

  if (collapsed) {
    return (
      <nav
        aria-label="Primary"
        className="flex flex-wrap gap-1 px-1 pb-2 md:flex-col md:items-center"
      >
        {groups.map((group) => (
          <button
            key={group}
            type="button"
            aria-label={`Open ${group}`}
            onClick={() => {
              setChoice({ path: pathname, group });
              onExpand?.();
            }}
            className={`group relative flex h-10 w-10 items-center justify-center rounded-md ${
              group === current
                ? 'bg-shi-mid-deep text-shi-ink'
                : 'text-slate-900 hover:bg-slate-50'
            }`}
          >
            <NavGroupIcon group={group} />
            <span
              aria-hidden="true"
              className="invisible absolute top-1/2 left-full z-30 ml-2 -translate-y-1/2 border border-slate-300 bg-slate-100 px-2 py-1 text-sm font-medium whitespace-nowrap text-slate-900 group-hover:visible group-focus-visible:visible"
            >
              {group}
            </span>
          </button>
        ))}
      </nav>
    );
  }

  return (
    <nav aria-label="Primary" className="pb-4">
      {groups.map((group) => {
        const open = group === openGroup;
        const listId = `nav-group-${group.toLowerCase().replace(/\W+/g, '-')}`;
        return (
          <div key={group} className="px-2 pb-1">
            <h2>
              <button
                type="button"
                aria-expanded={open}
                aria-controls={listId}
                onClick={() => toggle(group)}
                className={`flex min-h-9 w-full items-center justify-between rounded-md px-2 text-left text-sm font-bold ${
                  open ? 'bg-shi-mid-deep text-shi-ink' : 'text-slate-900 hover:bg-slate-50'
                }`}
              >
                {group}
                <svg
                  aria-hidden="true"
                  viewBox="0 0 12 12"
                  width="12"
                  height="12"
                  className={`shrink-0 motion-safe:transition-transform ${open ? 'rotate-90' : ''}`}
                >
                  <path d="M4 2 8 6 4 10" fill="none" stroke="currentColor" strokeWidth="1.75" />
                </svg>
              </button>
            </h2>
            <ul
              id={listId}
              hidden={!open}
              className="mt-1 mb-2 ml-1 border-l-[3px] border-shi-pink pl-1"
            >
              {items
                .filter((i) => i.group === group)
                .map((i) => {
                  const active = pathname === i.href;
                  return (
                    <li key={i.id}>
                      <Link
                        href={i.href}
                        aria-current={active ? 'page' : undefined}
                        className={`block rounded-md px-2 py-1.5 text-sm ${
                          active
                            ? 'bg-blue-50 font-semibold text-blue-900 shadow-[inset_3px_0_0_var(--brand-accent,transparent)]'
                            : 'text-slate-700 hover:bg-slate-50 hover:text-slate-900'
                        }`}
                      >
                        {i.label}
                      </Link>
                    </li>
                  );
                })}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}
