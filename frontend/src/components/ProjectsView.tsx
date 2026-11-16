'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useParams } from 'next/navigation';
import { endpoints } from '@/lib/api/endpoints';
import type { Project } from '@/lib/api/types';
import { useProjects } from '@/lib/hooks';
import { projHref } from '@/lib/scope';
import { CreateScopeForm } from './CreateScopeForm';
import { QueryError } from './QueryError';
import { ScopeListView } from './ScopeListView';

export function ProjectsView() {
  const params = useParams<{ org: string; ws: string }>();
  const org = decodeURIComponent(params.org);
  const ws = decodeURIComponent(params.ws);
  const queryClient = useQueryClient();
  const [showArchived, setShowArchived] = useState(false);
  const list = useProjects(org, ws, showArchived);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['projects', org, ws] });

  const create = useMutation({
    mutationFn: (v: { slug: string; name: string; description?: string }) =>
      endpoints.createProject(org, ws, v),
    onSuccess: refresh,
  });
  const archive = useMutation({
    mutationFn: (p: Project) => endpoints.archiveProject(org, ws, p),
    onSuccess: refresh,
  });

  if (list.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading projects...
      </p>
    );
  }
  if (list.isError) return <QueryError error={list.error} />;

  return (
    <ScopeListView
      title="Projects"
      noun="project"
      items={list.data.items}
      showArchived={showArchived}
      onShowArchivedChange={setShowArchived}
      hrefFor={(p) => projHref(org, ws, p.slug)}
      archive={(p) => archive.mutateAsync(p)}
      createForm={<CreateScopeForm noun="project" onSubmit={(v) => create.mutateAsync(v)} />}
    />
  );
}
