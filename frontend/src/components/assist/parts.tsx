'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import type { AssistRequestStatus } from '@/lib/api/assist-types';
import { REQUEST_STATUS } from '@/lib/assist';
import { assistSettingsHref } from '@/lib/scope';
import { Badge } from '../admin/ui';

export const linkClass = 'text-blue-800 underline';

/** One project, by its slugs. */
export interface ProjectScope {
  org: string;
  ws: string;
  proj: string;
}

export function RequestStatusBadge({ status }: { status: AssistRequestStatus }) {
  const { label, tone } = REQUEST_STATUS[status];
  return <Badge tone={tone}>{label}</Badge>;
}

/** One term and its value in a `dl`. */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-slate-600">{label}</dt>
      <dd className="break-words">{children}</dd>
    </>
  );
}

/** Where to set assist up: the settings for a user who can, otherwise an administrator. */
export function SettingsHint({ org, ws, proj, allowed }: ProjectScope & { allowed: boolean }) {
  return allowed ? (
    <Link className={linkClass} href={assistSettingsHref(org, ws, proj)}>
      Open the assist settings
    </Link>
  ) : (
    <>Ask an administrator.</>
  );
}
