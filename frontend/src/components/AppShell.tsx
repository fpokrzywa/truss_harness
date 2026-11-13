'use client';

import { usePathname, useRouter } from 'next/navigation';
import { useEffect, type ReactNode } from 'react';
import { ApiError } from '@/lib/api/errors';
import { loginRedirectUrl } from '@/lib/api/client';
import { useSession } from '@/lib/hooks';
import { parseScope } from '@/lib/scope';
import { AppBrand } from './AppBrand';
import { ErrorMessage } from './ErrorMessage';
import { ScopeSelectors } from './ScopeSelectors';
import { Sidebar } from './Sidebar';
import { SidebarFrame } from './SidebarFrame';
import { UserMenu } from './UserMenu';

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const session = useSession();
  const unauthenticated = session.error instanceof ApiError && session.error.status === 401;

  useEffect(() => {
    if (unauthenticated) router.replace(loginRedirectUrl(pathname));
  }, [unauthenticated, pathname, router]);

  if (session.isPending || unauthenticated) {
    return (
      <p role="status" className="p-8 text-sm text-slate-600">
        {unauthenticated ? 'Redirecting to sign in...' : 'Loading...'}
      </p>
    );
  }
  if (session.isError) {
    return (
      <div className="mx-auto max-w-xl p-8">
        <ErrorMessage error={session.error} title="Could not load your session" />
        <button type="button" className="btn mt-4" onClick={() => void session.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  const { user, memberships } = session.data;
  return (
    <div className="flex min-h-screen flex-col md:h-screen md:overflow-hidden">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:bg-slate-100 focus:p-2"
      >
        Skip to main content
      </a>
      <header className="flex flex-wrap items-center justify-between gap-x-12 gap-y-4 shrink-0 border-b border-slate-200 px-6 py-5">
        <AppBrand href="/" />
        <div className="flex flex-wrap items-center gap-3">
          <ScopeSelectors scope={parseScope(pathname)} memberships={memberships} />
          <UserMenu user={user} />
        </div>
      </header>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col md:flex-row">
        <SidebarFrame>
          {(rail) => (
            <Sidebar
              isPlatformAdmin={user.is_platform_admin}
              collapsed={rail.collapsed}
              onExpand={rail.expand}
            />
          )}
        </SidebarFrame>
        <main
          id="main"
          tabIndex={-1}
          className="min-w-0 flex-1 p-6 max-md:overflow-x-auto md:overflow-y-auto"
        >
          {children}
        </main>
      </div>
    </div>
  );
}
