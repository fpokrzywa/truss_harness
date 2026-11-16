'use client';

import type { BudgetScopeType, GovScope } from '@/lib/api/governance-types';
import { BUDGET_SCOPE_TYPES } from '@/lib/api/governance-types';
import { useArtifacts } from '@/lib/build-hooks';
import { useScopeOptions, useUsers } from '@/lib/security-hooks';
import { SelectField } from '../admin/ui';
import { useOrgId } from '../admin/ScopePicker';

export const SCOPE_LABEL: Record<BudgetScopeType, string> = {
  tenant: 'Whole organization',
  workspace: 'A workspace',
  project: 'A project',
  agent: 'An agent',
  pipeline: 'A pipeline',
  principal: 'A principal (user)',
};

/** Scope `{type, id}` for budgets and quotas: tenant, workspace, project, agent, pipeline or principal. */
export function GovScopePicker({
  org,
  value,
  onChange,
}: {
  org: string;
  value: GovScope;
  onChange: (scope: GovScope) => void;
}) {
  const orgId = useOrgId(org) ?? '';
  const { options } = useScopeOptions(org);
  const agents = useArtifacts(org, 'agents', { status: 'active' });
  const pipelines = useArtifacts(org, 'pipelines', { status: 'active' });
  const users = useUsers(org);

  const targets: { value: string; label: string }[] =
    value.type === 'workspace' || value.type === 'project'
      ? options.filter((o) => o.type === value.type).map((o) => ({ value: o.id, label: o.label }))
      : value.type === 'agent'
        ? (agents.data?.items ?? []).map((a) => ({ value: a.id, label: `${a.name} (${a.key})` }))
        : value.type === 'pipeline'
          ? (pipelines.data?.items ?? []).map((a) => ({
              value: a.id,
              label: `${a.name} (${a.key})`,
            }))
          : value.type === 'principal'
            ? (users.data?.items ?? []).map((u) => ({
                value: u.user.id,
                label: `${u.user.display_name} (${u.user.email})`,
              }))
            : [];

  function setType(t: string) {
    const type = t as BudgetScopeType;
    onChange({ type, id: type === 'tenant' ? orgId : '' });
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <SelectField
        label="Scope"
        value={value.type}
        onChange={setType}
        options={BUDGET_SCOPE_TYPES.map((t) => ({ value: t, label: SCOPE_LABEL[t] }))}
      />
      {value.type !== 'tenant' && (
        <SelectField
          label="Scope target"
          value={value.id}
          onChange={(id) => onChange({ type: value.type, id })}
          options={[{ value: '', label: 'Select...' }, ...targets]}
        />
      )}
    </div>
  );
}
