'use client';

import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useProjects, useWorkspaces } from '@/lib/hooks';
import { SelectField } from '../admin/ui';

/** The project of an organization-level release screen, from `?ws=<slug>&proj=<slug>`. */
export function useProjectQuery() {
  const params = useSearchParams();
  return { ws: params.get('ws') ?? '', proj: params.get('proj') ?? '' };
}

/** Workspace and project selects that write the choice into the query string. */
export function ProjectScopePicker({ org }: { org: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const { ws, proj } = useProjectQuery();
  const workspaces = useWorkspaces(org);
  const projects = useProjects(org, ws || undefined);

  function go(nextWs: string, nextProj: string) {
    const q = new URLSearchParams();
    if (nextWs) q.set('ws', nextWs);
    if (nextProj) q.set('proj', nextProj);
    const query = q.toString();
    router.replace(query ? `${pathname}?${query}` : pathname);
  }

  const wsOptions = (workspaces.data?.items ?? [])
    .filter((w) => w.status === 'active' || w.slug === ws)
    .map((w) => ({ value: w.slug, label: w.name }));
  const projOptions = ws
    ? (projects.data?.items ?? [])
        .filter((p) => p.status === 'active' || p.slug === proj)
        .map((p) => ({ value: p.slug, label: p.name }))
    : [];
  // Keep a slug from the URL selectable before (or without) its list loading.
  if (ws && !wsOptions.some((o) => o.value === ws)) wsOptions.push({ value: ws, label: ws });
  if (proj && !projOptions.some((o) => o.value === proj))
    projOptions.push({ value: proj, label: proj });

  return (
    <div className="grid max-w-xl gap-3 sm:grid-cols-2">
      <SelectField
        label="Workspace"
        value={ws}
        onChange={(v) => go(v, '')}
        options={[{ value: '', label: 'Select...' }, ...wsOptions]}
      />
      <SelectField
        label="Project"
        value={proj}
        disabled={ws === ''}
        onChange={(v) => go(ws, v)}
        options={[{ value: '', label: 'Select...' }, ...projOptions]}
      />
    </div>
  );
}
