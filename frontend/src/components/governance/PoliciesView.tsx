'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { POLICY_KINDS, type GovernancePolicy, type PolicyKind } from '@/lib/api/governance-types';
import { useCan } from '@/lib/hooks';
import { govKey, useGovPolicies } from '@/lib/governance-hooks';
import {
  ActionError,
  Badge,
  EmptyState,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { GovScopePicker } from './GovScopePicker';

export const KIND_LABEL: Record<PolicyKind, string> = {
  model_policy: 'Model policy',
  approval_rule: 'Approval rule',
  data_export: 'Data export',
  lifecycle: 'Lifecycle',
  cost_threshold: 'Cost threshold',
};

const KIND_HELP: Record<PolicyKind, string> = {
  model_policy: 'Model allowlists by scope (the same allowlists as Integrate > Models).',
  approval_rule: 'Require approval when estimated cost exceeds a limit or for tool classes.',
  data_export: 'Rules for exporting data.',
  lifecycle:
    'Auto-deprecate unused versions after N days. Not enforced: stored and reported only, no job evaluates it yet.',
  cost_threshold: 'Alert or require approval when a run exceeds a cost threshold.',
};

/** Tool side-effect classes (backend governance/validation.py SIDE_EFFECT_CLASSES). */
export const TOOL_CLASSES = [
  'none',
  'read',
  'write_idempotent',
  'write_non_idempotent',
  'destructive',
] as const;

/** Artifact kinds a lifecycle policy may name (backend validation.ARTIFACT_KINDS). */
export const ARTIFACT_KINDS = [
  'prompts',
  'tools',
  'skills',
  'agents',
  'orchestrations',
  'pipelines',
  'ontologies',
] as const;

type Def = Record<string, unknown>;
const STRUCTURED: readonly PolicyKind[] = ['cost_threshold', 'lifecycle', 'approval_rule'];
const numOf = (v: unknown): string =>
  typeof v === 'number' ? String(v) : typeof v === 'string' ? v : '';
const whenOf = (d: Def): Def =>
  typeof d.when === 'object' && d.when !== null && !Array.isArray(d.when) ? (d.when as Def) : {};

/** Definition validation for the structured forms (mirrors backend governance/validation.py). */
export function definitionErrors(kind: PolicyKind, d: Def): string[] {
  const out: string[] = [];
  const posNum = (v: unknown) => {
    const n = typeof v === 'string' && v.trim() !== '' ? Number(v) : v;
    return typeof n === 'number' && Number.isFinite(n) && n > 0;
  };
  const cur = (v: unknown) => typeof v === 'string' && /^[A-Z]{3}$/.test(v);
  if (kind === 'cost_threshold') {
    if (!posNum(d.threshold_amount)) out.push('Threshold must be a positive number.');
    if (!cur(d.currency)) out.push('Currency must be a 3-letter upper-case code.');
  }
  if (kind === 'lifecycle') {
    const kinds = Array.isArray(d.artifact_kinds) ? d.artifact_kinds : [];
    if (kinds.length === 0) out.push('Choose at least one artifact kind.');
    const days = d.deprecate_unused_after_days;
    if (!(typeof days === 'number' && Number.isInteger(days) && days >= 1 && days <= 3650))
      out.push('Unused days must be a whole number from 1 to 3650.');
  }
  if (kind === 'approval_rule') {
    const when = whenOf(d);
    const hasCost = when.estimated_cost_gt !== undefined;
    const classes = Array.isArray(when.side_effect_classes) ? when.side_effect_classes : [];
    if (!hasCost && classes.length === 0)
      out.push('Set a cost limit or choose at least one tool class.');
    if (hasCost && !posNum(when.estimated_cost_gt))
      out.push('Cost limit must be a positive number.');
    if (hasCost && !cur(when.currency)) out.push('Currency must be a 3-letter upper-case code.');
  }
  return out;
}

export function defaultDefinition(kind: PolicyKind): Def {
  switch (kind) {
    case 'cost_threshold':
      return { threshold_amount: 10, currency: 'USD', action: 'alert' };
    case 'lifecycle':
      return { artifact_kinds: ['prompts'], deprecate_unused_after_days: 90 };
    case 'approval_rule':
      return { when: { side_effect_classes: ['destructive'] } };
    default:
      return {};
  }
}

export function PoliciesView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const manage = can('governance:manage');
  const policies = useGovPolicies(org);
  const [kind, setKind] = useState<PolicyKind | ''>('');
  const [dialog, setDialog] = useState<{ policy?: GovernancePolicy } | null>(null);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Governance policies"
        description="Policies that govern model use, approvals, exports, lifecycle and cost. Every change is audited."
        actions={
          manage ? (
            <button type="button" className="btn btn-primary" onClick={() => setDialog({})}>
              Create policy
            </button>
          ) : undefined
        }
      />
      <div className="max-w-xs">
        <SelectField
          label="Kind"
          value={kind}
          onChange={(v) => setKind(v as PolicyKind | '')}
          options={[
            { value: '', label: 'All kinds' },
            ...POLICY_KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] })),
          ]}
        />
      </div>
      <QueryGate query={policies} noun="policies">
        {(all) => {
          const items = kind ? all.filter((p) => p.kind === kind) : all;
          return items.length === 0 ? (
            <section className="card p-0">
              <EmptyState>No policies of this kind.</EmptyState>
            </section>
          ) : (
            <section aria-label="Policies" className="card overflow-x-auto p-0">
              <table className="w-full">
                <caption className="sr-only">Governance policies</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Name', 'Kind', 'Status', 'Definition', ''].map((h, i) => (
                      <th key={`${h}-${i}`} scope="col" className="th">
                        {h || <span className="sr-only">Actions</span>}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((p) => (
                    <tr key={p.id}>
                      <th scope="row" className="td text-left font-normal">
                        {p.name ?? p.id}
                      </th>
                      <td className="td">{KIND_LABEL[p.kind] ?? p.kind}</td>
                      <td className="td">
                        <Badge tone={p.status === 'active' ? 'good' : 'neutral'}>{p.status}</Badge>
                        {(p.enforcement === 'not_enforced' ||
                          (p.enforcement === undefined && p.kind === 'lifecycle')) && (
                          <span className="ml-1">
                            <Badge tone="warn">Not enforced</Badge>
                          </span>
                        )}
                      </td>
                      <td className="td font-mono text-xs">{JSON.stringify(p.definition)}</td>
                      <td className="td">
                        {manage && (
                          <button
                            type="button"
                            className="btn"
                            aria-label={`Edit policy ${p.name ?? p.id}`}
                            onClick={() => setDialog({ policy: p })}
                          >
                            Edit
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          );
        }}
      </QueryGate>
      {dialog && <PolicyDialog org={org} policy={dialog.policy} onClose={() => setDialog(null)} />}
    </div>
  );
}

function PolicyDialog({
  org,
  policy,
  onClose,
}: {
  org: string;
  policy?: GovernancePolicy;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const act = useAction();
  const [kind, setKind] = useState<PolicyKind>(policy?.kind ?? 'cost_threshold');
  const [name, setName] = useState(policy?.name ?? '');
  const [status, setStatus] = useState(policy?.status ?? 'active');
  const [scope, setScope] = useState(policy?.scope ?? { type: 'tenant' as const, id: '' });
  const [def, setDef] = useState<Def>(policy?.definition ?? defaultDefinition('cost_threshold'));
  const [jsonMode, setJsonMode] = useState(!!policy && !STRUCTURED.includes(policy.kind));
  const [jsonText, setJsonText] = useState(JSON.stringify(def, null, 2));

  const structured = STRUCTURED.includes(kind);
  const showJson = jsonMode || !structured;
  let jsonError: string | null = null;
  let effective: Def = def;
  if (showJson) {
    try {
      const parsed: unknown = JSON.parse(jsonText);
      if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed))
        effective = parsed as Def;
      else jsonError = 'The definition must be a JSON object.';
    } catch {
      jsonError = 'The definition is not valid JSON.';
    }
  }
  const problems = jsonError ? [jsonError] : structured ? definitionErrors(kind, effective) : [];
  const valid = problems.length === 0 && (scope.type === 'tenant' || !!scope.id);

  function pickKind(k: PolicyKind) {
    setKind(k);
    const d = defaultDefinition(k);
    setDef(d);
    setJsonText(JSON.stringify(d, null, 2));
  }

  function patch(change: Def) {
    const next = { ...def, ...change };
    for (const k of Object.keys(next)) if (next[k] === undefined) delete next[k];
    setDef(next);
    setJsonText(JSON.stringify(next, null, 2));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    const input = {
      kind,
      ...(name.trim() ? { name: name.trim() } : {}),
      ...(scope.type === 'tenant' ? {} : { scope }),
      definition: effective,
      status,
    };
    const r = await act.run(() =>
      policy
        ? endpoints.updateGovPolicy(org, policy, input)
        : endpoints.createGovPolicy(org, input),
    );
    if (r.ok) {
      await qc.invalidateQueries({ queryKey: govKey(org, 'policies') });
      onClose();
    }
  }

  return (
    <Modal title={policy ? 'Edit policy' : 'Create policy'} onClose={onClose} busy={act.busy} wide>
      <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
        <SelectField
          label="Policy kind"
          value={kind}
          disabled={!!policy}
          onChange={(v) => pickKind(v as PolicyKind)}
          options={POLICY_KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] }))}
        />
        <p className="text-xs text-slate-600">{KIND_HELP[kind]}</p>
        <TextField label="Name" value={name} onChange={setName} />
        <SelectField
          label="Policy status"
          value={status}
          onChange={setStatus}
          options={[
            { value: 'active', label: 'Active' },
            { value: 'disabled', label: 'Disabled' },
          ]}
        />
        <GovScopePicker org={org} value={scope} onChange={setScope} />

        {structured && !showJson && <StructuredFields kind={kind} def={def} patch={patch} />}
        {structured && (
          <button type="button" className="btn" onClick={() => setJsonMode((m) => !m)}>
            {jsonMode ? 'Use the form' : 'Edit as JSON'}
          </button>
        )}
        {showJson && (
          <TextField
            label="Definition (JSON object)"
            value={jsonText}
            onChange={setJsonText}
            multiline
            monospace
            error={jsonError}
          />
        )}
        {!jsonError && problems.length > 0 && (
          <ul role="alert" className="list-disc pl-5 text-sm text-red-700">
            {problems.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        )}
        <ActionError error={act.error} title="Could not save the policy" />
        <ModalActions
          onCancel={onClose}
          submitLabel={policy ? 'Save policy' : 'Create policy'}
          busy={act.busy}
          submitDisabled={!valid}
        />
      </form>
    </Modal>
  );
}

function StructuredFields({
  kind,
  def,
  patch,
}: {
  kind: PolicyKind;
  def: Def;
  patch: (change: Def) => void;
}) {
  const parse = (s: string) => (s.trim() === '' ? undefined : Number(s));
  const toggle = (list: string[], v: string, on: boolean) =>
    on ? [...list, v] : list.filter((x) => x !== v);
  if (kind === 'cost_threshold')
    return (
      <div className="grid gap-3 sm:grid-cols-3">
        <TextField
          label="Threshold per run"
          type="number"
          value={numOf(def.threshold_amount)}
          onChange={(v) => patch({ threshold_amount: parse(v) })}
        />
        <TextField
          label="Currency"
          value={typeof def.currency === 'string' ? def.currency : ''}
          onChange={(v) => patch({ currency: v.toUpperCase() })}
          maxLength={3}
        />
        <SelectField
          label="When exceeded"
          value={typeof def.action === 'string' ? def.action : 'alert'}
          onChange={(v) => patch({ action: v })}
          options={[
            { value: 'alert', label: 'Alert' },
            { value: 'require_approval', label: 'Require approval' },
          ]}
        />
      </div>
    );
  if (kind === 'lifecycle') {
    const kinds = Array.isArray(def.artifact_kinds) ? (def.artifact_kinds as string[]) : [];
    return (
      <div className="space-y-3">
        <fieldset>
          <legend className="text-sm font-medium">Artifact kinds</legend>
          <div className="mt-1 flex flex-wrap gap-3">
            {ARTIFACT_KINDS.map((k) => (
              <label key={k} className="flex items-center gap-1 text-sm">
                <input
                  type="checkbox"
                  checked={kinds.includes(k)}
                  onChange={(e) => patch({ artifact_kinds: toggle(kinds, k, e.target.checked) })}
                />
                {k}
              </label>
            ))}
          </div>
        </fieldset>
        <TextField
          label="Deprecate versions unused for (days)"
          type="number"
          value={numOf(def.deprecate_unused_after_days)}
          onChange={(v) => patch({ deprecate_unused_after_days: parse(v) })}
          help="Not enforced: this policy is stored and reported, but no job deprecates anything yet."
        />
      </div>
    );
  }
  const when = whenOf(def);
  const classes = Array.isArray(when.side_effect_classes)
    ? (when.side_effect_classes as string[])
    : [];
  // Rebuild `when` so an emptied field is dropped (the backend rejects a currency without a cost).
  const patchWhen = (change: Def) => {
    const next: Def = { ...when, ...change };
    for (const k of Object.keys(next)) if (next[k] === undefined) delete next[k];
    if (next.estimated_cost_gt === undefined) delete next.currency;
    patch({ when: next });
  };
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <TextField
          label="Require approval above estimated cost (optional)"
          type="number"
          value={numOf(when.estimated_cost_gt)}
          onChange={(v) => {
            const n = parse(v);
            patchWhen({
              estimated_cost_gt: n,
              ...(n !== undefined && when.currency === undefined ? { currency: 'USD' } : {}),
            });
          }}
        />
        {when.estimated_cost_gt !== undefined && (
          <TextField
            label="Cost currency"
            value={typeof when.currency === 'string' ? when.currency : ''}
            onChange={(v) => patchWhen({ currency: v.toUpperCase() })}
            maxLength={3}
          />
        )}
      </div>
      <fieldset>
        <legend className="text-sm font-medium">Require approval for tool classes</legend>
        <div className="mt-1 flex flex-wrap gap-3">
          {TOOL_CLASSES.map((c) => (
            <label key={c} className="flex items-center gap-1 text-sm">
              <input
                type="checkbox"
                checked={classes.includes(c)}
                onChange={(e) =>
                  patchWhen({ side_effect_classes: toggle(classes, c, e.target.checked) })
                }
              />
              {c}
            </label>
          ))}
        </div>
      </fieldset>
      <TextField
        label="Approver permission (optional)"
        value={typeof def.approver_permission === 'string' ? def.approver_permission : ''}
        onChange={(v) => patch({ approver_permission: v.trim() === '' ? undefined : v.trim() })}
        help="An existing permission key; leave empty for the default approvers."
      />
    </div>
  );
}
