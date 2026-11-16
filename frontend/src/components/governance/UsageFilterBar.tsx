'use client';

import type { CostKind, GroupBy, UsageFilters } from '@/lib/api/governance-types';
import { COST_KINDS, GROUP_BY } from '@/lib/api/governance-types';
import { endOfDay, startOfDay } from '@/lib/governance';
import { SelectField, TextField } from '../admin/ui';
import { SlugScopeSelect } from '../integrate/SlugScopeSelect';

export interface FilterState {
  groupBy: GroupBy;
  workspace: string;
  project: string;
  costKind: CostKind | '';
  from: string;
  to: string;
}

export const initialFilters = (groupBy: GroupBy): FilterState => ({
  groupBy,
  workspace: '',
  project: '',
  costKind: '',
  from: '',
  to: '',
});

/** Query filters for the summary/export endpoints from the form state (empty values are omitted). */
export function toUsageFilters(f: FilterState): UsageFilters {
  return {
    group_by: f.groupBy,
    from: startOfDay(f.from),
    to: endOfDay(f.to),
    workspace: f.workspace || undefined,
    project: f.project || undefined,
    cost_kind: f.costKind || undefined,
  };
}

const KIND_FILTER_LABEL: Record<CostKind, string> = {
  provider_reported: 'Provider-reported',
  calculated: 'Calculated from rate card',
  estimated_infra: 'Estimated infrastructure',
  unmeasured: 'Unmeasured',
};

const GROUP_LABEL: Record<GroupBy, string> = {
  workspace: 'Workspace',
  project: 'Project',
  user: 'User',
  agent: 'Agent',
  prompt: 'Prompt',
  model: 'Model',
  pipeline: 'Pipeline',
  workflow: 'Workflow',
  run: 'Run',
  build: 'Build',
};

export function UsageFilterBar({
  org,
  value,
  onChange,
  groupBy = true,
  scope = true,
  costKind = false,
}: {
  org: string;
  value: FilterState;
  onChange: (next: FilterState) => void;
  groupBy?: boolean;
  scope?: boolean;
  costKind?: boolean;
}) {
  const set = (patch: Partial<FilterState>) => onChange({ ...value, ...patch });
  return (
    <form
      aria-label="Filters"
      className="card grid gap-3 sm:grid-cols-2 lg:grid-cols-4"
      onSubmit={(e) => e.preventDefault()}
    >
      {groupBy && (
        <SelectField
          label="Group by"
          value={value.groupBy}
          onChange={(v) => set({ groupBy: v as GroupBy })}
          options={GROUP_BY.map((g) => ({ value: g, label: GROUP_LABEL[g] }))}
        />
      )}
      {costKind && (
        <SelectField
          label="Cost kind"
          value={value.costKind}
          onChange={(v) => set({ costKind: v as CostKind | '' })}
          options={[
            { value: '', label: 'All kinds' },
            ...COST_KINDS.map((k) => ({ value: k, label: KIND_FILTER_LABEL[k] })),
          ]}
        />
      )}
      <TextField
        label="From date"
        type="date"
        value={value.from}
        onChange={(v) => set({ from: v })}
      />
      <TextField label="To date" type="date" value={value.to} onChange={(v) => set({ to: v })} />
      {scope && (
        <div className="sm:col-span-2 lg:col-span-4">
          <SlugScopeSelect
            org={org}
            workspace={value.workspace}
            project={value.project}
            onChange={(s) => set({ workspace: s.workspace, project: s.project })}
          />
        </div>
      )}
    </form>
  );
}
