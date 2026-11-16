'use client';

import { useProjects, useWorkspaces } from '@/lib/hooks';
import { SelectField } from '../admin/ui';

/**
 * Optional workspace and project selection by slug (the convention of `?workspace=&project=` in
 * P1/P2 routes). Empty workspace means the whole organization. Choosing a workspace resets the project.
 */
export function SlugScopeSelect({
  org,
  workspace,
  project,
  onChange,
  idPrefix = '',
}: {
  org: string;
  workspace: string;
  project: string;
  onChange: (next: { workspace: string; project: string }) => void;
  idPrefix?: string;
}) {
  const workspaces = useWorkspaces(org);
  const projects = useProjects(org, workspace || undefined);
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <SelectField
        label={`${idPrefix}Workspace`}
        value={workspace}
        onChange={(w) => onChange({ workspace: w, project: '' })}
        options={[
          { value: '', label: 'Whole organization' },
          ...(workspaces.data?.items ?? []).map((w) => ({ value: w.slug, label: w.name })),
        ]}
      />
      {workspace !== '' && (
        <SelectField
          label={`${idPrefix}Project`}
          value={project}
          onChange={(p) => onChange({ workspace, project: p })}
          options={[
            { value: '', label: 'Whole workspace' },
            ...(projects.data?.items ?? []).map((p) => ({ value: p.slug, label: p.name })),
          ]}
        />
      )}
    </div>
  );
}
