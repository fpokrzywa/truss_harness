'use client';

import { useRouter } from 'next/navigation';
import { useId } from 'react';
import { useProjects, useWorkspaces } from '@/lib/hooks';
import { scopeChangeTarget, type ScopeLevel, type ScopePath } from '@/lib/scope';
import type { Membership } from '@/lib/api/types';

interface Option {
  value: string;
  label: string;
}

interface ScopeSelectProps {
  label: string;
  value: string;
  options: Option[];
  /** Label for the empty ("no selection") option; omit when selection is mandatory. */
  emptyLabel?: string;
  disabled?: boolean;
  loading?: boolean;
  onChange: (value: string) => void;
}

export function ScopeSelect({
  label,
  value,
  options,
  emptyLabel,
  disabled,
  loading,
  onChange,
}: ScopeSelectProps) {
  const id = useId();
  // Keep the current slug visible even if it is not (yet) in the list, e.g. while loading.
  const hasValue = value === '' || options.some((o) => o.value === value);
  return (
    <div className="flex items-center gap-2">
      <label htmlFor={id} className="text-xs font-medium text-slate-600">
        {label}
      </label>
      <select
        id={id}
        className="input w-auto min-w-36 py-1"
        value={value}
        disabled={disabled}
        aria-busy={loading || undefined}
        onChange={(e) => onChange(e.target.value)}
      >
        {emptyLabel !== undefined && <option value="">{emptyLabel}</option>}
        {!hasValue && <option value={value}>{value}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

/** Organization / Workspace / Project selectors driven by real API data; selection drives the URL. */
export function ScopeSelectors({
  scope,
  memberships,
}: {
  scope: ScopePath;
  memberships: Membership[];
}) {
  const router = useRouter();
  const workspaces = useWorkspaces(scope.org);
  const projects = useProjects(scope.org, scope.ws);

  const orgOptions = memberships
    .filter((m) => m.org.status === 'active' || m.org.slug === scope.org)
    .map((m) => ({ value: m.org.slug, label: m.org.name }));
  // Archived scopes are excluded from selectors unless currently selected (FR-TEN-009).
  const wsOptions = (workspaces.data?.items ?? [])
    .filter((w) => w.status === 'active' || w.slug === scope.ws)
    .map((w) => ({ value: w.slug, label: w.name }));
  const projOptions = (projects.data?.items ?? [])
    .filter((p) => p.status === 'active' || p.slug === scope.proj)
    .map((p) => ({ value: p.slug, label: p.name }));

  function go(level: ScopeLevel, value: string) {
    const target = scopeChangeTarget(scope, level, value);
    if (target) router.push(target);
  }

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2" role="group" aria-label="Scope">
      <ScopeSelect
        label="Organization"
        value={scope.org ?? ''}
        options={orgOptions}
        emptyLabel={scope.org ? undefined : 'Select organization'}
        onChange={(v) => go('org', v)}
      />
      {scope.org && (
        <ScopeSelect
          label="Workspace"
          value={scope.ws ?? ''}
          options={wsOptions}
          emptyLabel="All workspaces"
          loading={workspaces.isPending}
          onChange={(v) => go('ws', v)}
        />
      )}
      {scope.org && scope.ws && (
        <ScopeSelect
          label="Project"
          value={scope.proj ?? ''}
          options={projOptions}
          emptyLabel="All projects"
          loading={projects.isPending}
          onChange={(v) => go('proj', v)}
        />
      )}
    </div>
  );
}
