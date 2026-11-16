'use client';

import { useSession } from '@/lib/hooks';
import type { BindingScope } from '@/lib/api/security-types';
import { useScopeOptions } from '@/lib/security-hooks';
import { SelectField } from './ui';

type Level = 'tenant' | 'workspace' | 'project';

export function useOrgId(org: string): string | undefined {
  const session = useSession();
  return session.data?.memberships.find((m) => m.org.slug === org)?.org.id;
}

/**
 * Picks a tenant, workspace or project scope. The scope id is the object's UUID (tenant = org id),
 * matching how the contract references subjects and roles in bodies.
 */
export function ScopePicker({
  org,
  value,
  onChange,
  tenantLabel = 'Whole organization',
}: {
  org: string;
  value: Pick<BindingScope, 'type' | 'id'>;
  onChange: (scope: { type: Level; id: string }) => void;
  tenantLabel?: string;
}) {
  const orgId = useOrgId(org) ?? '';
  const { options } = useScopeOptions(org);
  const level = (
    value.type === 'workspace' || value.type === 'project' ? value.type : 'tenant'
  ) as Level;
  const targets = options.filter((o) => o.type === level);

  function setLevel(next: string) {
    const l = next as Level;
    if (l === 'tenant') onChange({ type: 'tenant', id: orgId });
    else onChange({ type: l, id: options.find((o) => o.type === l)?.id ?? '' });
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <SelectField
        label="Scope"
        value={level}
        onChange={setLevel}
        options={[
          { value: 'tenant', label: tenantLabel },
          { value: 'workspace', label: 'A workspace' },
          { value: 'project', label: 'A project' },
        ]}
      />
      {level !== 'tenant' && (
        <SelectField
          label={level === 'workspace' ? 'Workspace' : 'Project'}
          value={value.id}
          onChange={(id) => onChange({ type: level, id })}
          options={[
            { value: '', label: `Select a ${level}...` },
            ...targets.map((o) => ({ value: o.id, label: o.label })),
          ]}
        />
      )}
    </div>
  );
}
