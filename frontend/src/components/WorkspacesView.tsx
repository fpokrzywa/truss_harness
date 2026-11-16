'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { endpoints } from '@/lib/api/endpoints';
import type { Workspace } from '@/lib/api/types';
import { useWorkspaces } from '@/lib/hooks';
import { projectsHref, wsHref } from '@/lib/scope';
import { CreateScopeForm } from './CreateScopeForm';
import { QueryError } from './QueryError';
import { ScopeListView } from './ScopeListView';

export function WorkspacesView() {
  const org = decodeURIComponent(useParams<{ org: string }>().org);
  const queryClient = useQueryClient();
  const [showArchived, setShowArchived] = useState(false);
  const list = useWorkspaces(org, showArchived);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['workspaces', org] });

  const create = useMutation({
    mutationFn: (v: { slug: string; name: string; description?: string }) =>
      endpoints.createWorkspace(org, v),
    onSuccess: refresh,
  });
  const archive = useMutation({
    mutationFn: (ws: Workspace) => endpoints.archiveWorkspace(org, ws),
    onSuccess: refresh,
  });

  if (list.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading workspaces...
      </p>
    );
  }
  if (list.isError) return <QueryError error={list.error} />;

  return (
    <ScopeListView
      title="Workspaces"
      noun="workspace"
      items={list.data.items}
      showArchived={showArchived}
      onShowArchivedChange={setShowArchived}
      hrefFor={(w) => wsHref(org, w.slug)}
      extraActions={(w) => (
        <Link href={projectsHref(org, w.slug)} className="btn">
          Projects <span className="sr-only">in {w.name}</span>
        </Link>
      )}
      archive={(w) => archive.mutateAsync(w)}
      createForm={<CreateScopeForm noun="workspace" onSubmit={(v) => create.mutateAsync(v)} />}
    />
  );
}
