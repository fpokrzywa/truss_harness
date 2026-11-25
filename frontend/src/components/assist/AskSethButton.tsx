'use client';

import Link from 'next/link';
import type { AssistContext } from '@/lib/api/assist-types';
import { usePermissions } from '@/lib/hooks';
import { assistHref } from '@/lib/scope';

/** Opens the project's Ask SETH screen about one object; nothing without `assist:request:write`. */
export function AskSethButton({
  org,
  ws,
  proj,
  context,
}: {
  org: string;
  ws: string;
  proj: string;
  context: AssistContext;
}) {
  const permissions = usePermissions(org, ws, proj);
  if (!permissions.data?.has('assist:request:write')) return null;
  return (
    <Link className="btn" href={assistHref(org, ws, proj, context)}>
      Ask SETH
    </Link>
  );
}
