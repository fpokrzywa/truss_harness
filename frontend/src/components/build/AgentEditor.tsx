'use client';

import { useState } from 'react';
import type { AgentSpec, SideEffectClass } from '@/lib/api/build-types';
import {
  APPROVABLE,
  DEFAULT_APPROVALS,
  defaultAgentSpec,
  REQUIRED_APPROVAL,
  withDestructive,
} from '@/lib/agent-spec';
import { useRoutes } from '@/lib/integrate-hooks';
import { SelectField, TextField } from '../admin/ui';
import type { EditorProps } from './DraftEditor';
import { ModelSelect, VersionPicker, VersionRefList } from './pickers';
import { Area, Check, num, NumberField, Section } from './shared';

export function normalizeAgentSpec(raw: Record<string, unknown> | undefined): AgentSpec {
  const d = defaultAgentSpec();
  if (!raw) return d;
  const r = raw as Partial<AgentSpec>;
  return {
    ...d,
    ...r,
    model: r.model ?? d.model,
    tool_version_ids: r.tool_version_ids ?? [],
    skill_version_ids: r.skill_version_ids ?? [],
    memory: { ...d.memory, ...(r.memory ?? {}) },
    limits: { ...d.limits, ...(r.limits ?? {}) },
    execution_permissions: { ...d.execution_permissions, ...(r.execution_permissions ?? {}) },
    approval_rules: {
      require_approval_for: withDestructive(
        r.approval_rules?.require_approval_for ?? DEFAULT_APPROVALS,
      ),
    },
    variables_map: r.variables_map ?? {},
  };
}

/** The payload sent on save: destructive approval is always present regardless of UI state. */
export function agentPayload(spec: AgentSpec): AgentSpec {
  return {
    ...spec,
    approval_rules: {
      require_approval_for: withDestructive(spec.approval_rules.require_approval_for),
    },
  };
}

export const agentBlockers = (spec: AgentSpec): string[] => {
  const errs: string[] = [];
  if (!spec.prompt_version_id) errs.push('Select a prompt (latest published or a pinned version).');
  if ('model_id' in spec.model ? !spec.model.model_id : !spec.model.route_id)
    errs.push('Select a model or a model route.');
  return errs;
};

const mapToLines = (m: Record<string, string>) =>
  Object.entries(m)
    .map(([k, v]) => `${k}=${v}`)
    .join('\n');
const linesToMap = (s: string): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const line of s.split('\n')) {
    const i = line.indexOf('=');
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
};

/** Keeps its own text so incomplete lines survive while typing; commits the parsed map. */
function MapField({
  value,
  onChange,
  readOnly,
}: {
  value: Record<string, string>;
  onChange: (m: Record<string, string>) => void;
  readOnly: boolean;
}) {
  const [text, setText] = useState(() => mapToLines(value));
  return (
    <Area
      label="Variables map"
      rows={4}
      monospace
      readOnly={readOnly}
      value={text}
      onChange={(v) => {
        setText(v);
        onChange(linesToMap(v));
      }}
    />
  );
}

export function AgentEditor({ org, spec, onChange, readOnly }: EditorProps<AgentSpec>) {
  const set = (patch: Partial<AgentSpec>) => onChange({ ...spec, ...patch });
  const routes = useRoutes(org);
  const useRoute = 'route_id' in spec.model;
  const modelSpec =
    'model_id' in spec.model ? spec.model : { model_id: '', fallback_model_ids: [] };
  const principals = spec.execution_permissions.allowed_principal_types;

  const togglePrincipal = (p: 'user' | 'service_account', on: boolean) =>
    set({
      execution_permissions: {
        ...spec.execution_permissions,
        allowed_principal_types: on
          ? [...new Set([...principals, p])]
          : principals.filter((x) => x !== p),
      },
    });

  const toggleApproval = (c: SideEffectClass, on: boolean) =>
    set({
      approval_rules: {
        require_approval_for: withDestructive(
          on
            ? [...new Set([...spec.approval_rules.require_approval_for, c])]
            : spec.approval_rules.require_approval_for.filter((x) => x !== c),
        ),
      },
    });

  return (
    <div className="space-y-4">
      <Section
        title="Prompt"
        help='"Latest published" is resolved and pinned to a version id when you publish the agent, so published agents are reproducible.'
      >
        <VersionPicker
          org={org}
          kind="prompts"
          label="Prompt"
          value={spec.prompt_version_id}
          allowLatest
          disabled={readOnly}
          onChange={(v) => set({ prompt_version_id: v })}
        />
      </Section>

      <Section title="Model">
        <SelectField
          label="Model selection"
          value={useRoute ? 'route' : 'model'}
          disabled={readOnly}
          onChange={(m) =>
            set({
              model: m === 'route' ? { route_id: '' } : { model_id: '', fallback_model_ids: [] },
            })
          }
          options={[
            { value: 'model', label: 'A model with ordered fallbacks' },
            { value: 'route', label: 'A model route' },
          ]}
        />
        {useRoute && 'route_id' in spec.model ? (
          <SelectField
            label="Model route"
            value={spec.model.route_id}
            disabled={readOnly}
            onChange={(id) => set({ model: { route_id: id } })}
            options={[
              { value: '', label: 'Select a route...' },
              ...(routes.data?.items ?? []).map((r) => ({ value: r.id, label: r.name })),
            ]}
          />
        ) : (
          <>
            <ModelSelect
              org={org}
              label="Model"
              value={modelSpec.model_id}
              disabled={readOnly}
              onChange={(id) => set({ model: { ...modelSpec, model_id: id } })}
            />
            {modelSpec.fallback_model_ids.map((fid, i) => (
              <div key={i} className="flex flex-wrap items-end gap-2">
                <ModelSelect
                  org={org}
                  label={`Fallback ${i + 1}`}
                  value={fid}
                  disabled={readOnly}
                  onChange={(id) =>
                    set({
                      model: {
                        ...modelSpec,
                        fallback_model_ids: modelSpec.fallback_model_ids.map((x, j) =>
                          j === i ? id : x,
                        ),
                      },
                    })
                  }
                />
                {!readOnly && (
                  <button
                    type="button"
                    className="btn"
                    onClick={() =>
                      set({
                        model: {
                          ...modelSpec,
                          fallback_model_ids: modelSpec.fallback_model_ids.filter(
                            (_, j) => j !== i,
                          ),
                        },
                      })
                    }
                  >
                    Remove fallback {i + 1}
                  </button>
                )}
              </div>
            ))}
            {!readOnly && (
              <button
                type="button"
                className="btn"
                onClick={() =>
                  set({
                    model: {
                      ...modelSpec,
                      fallback_model_ids: [...modelSpec.fallback_model_ids, ''],
                    },
                  })
                }
              >
                Add fallback model
              </button>
            )}
          </>
        )}
      </Section>

      <Section
        title="Tools"
        help="Tools the agent may declare. Pin a version or follow the latest published."
      >
        <VersionRefList
          org={org}
          kind="tools"
          label="Tool"
          values={spec.tool_version_ids}
          allowLatest
          readOnly={readOnly}
          onChange={(v) => set({ tool_version_ids: v })}
        />
      </Section>

      <Section title="Skills">
        <VersionRefList
          org={org}
          kind="skills"
          label="Skill"
          values={spec.skill_version_ids}
          allowLatest
          readOnly={readOnly}
          onChange={(v) => set({ skill_version_ids: v })}
        />
      </Section>

      <Section title="Memory">
        <div className="grid gap-3 sm:grid-cols-2">
          <SelectField
            label="Memory mode"
            value={spec.memory.mode}
            disabled={readOnly}
            onChange={(m) =>
              set({ memory: { ...spec.memory, mode: m as AgentSpec['memory']['mode'] } })
            }
            options={[
              { value: 'none', label: 'none' },
              { value: 'run', label: 'run' },
              { value: 'session', label: 'session' },
            ]}
          />
          <NumberField
            label="Max turns"
            min={0}
            fallback={0}
            disabled={readOnly}
            value={spec.memory.max_turns}
            onChange={(v) => set({ memory: { ...spec.memory, max_turns: v } })}
          />
        </div>
      </Section>

      <Section title="Limits">
        <div className="grid gap-3 sm:grid-cols-3">
          {(
            [
              ['max_iterations', 'Max iterations'],
              ['max_tool_calls', 'Max tool calls'],
              ['max_tokens', 'Max tokens'],
              ['deadline_s', 'Deadline (seconds)'],
              ['max_cost', 'Max cost (enforced from a later phase)'],
            ] as const
          ).map(([k, label]) => (
            <TextField
              key={k}
              label={label}
              type="number"
              min={0}
              disabled={readOnly}
              value={spec.limits[k] === undefined ? '' : String(spec.limits[k])}
              onChange={(v) =>
                set({ limits: { ...spec.limits, [k]: num(v) } as AgentSpec['limits'] })
              }
            />
          ))}
        </div>
      </Section>

      <Section title="Execution permissions">
        <Check
          label="Users may execute"
          checked={principals.includes('user')}
          disabled={readOnly}
          onChange={(on) => togglePrincipal('user', on)}
        />
        <Check
          label="Service accounts may execute"
          checked={principals.includes('service_account')}
          disabled={readOnly}
          onChange={(on) => togglePrincipal('service_account', on)}
        />
        <p className="text-xs text-slate-600">
          Required permission: <code>{spec.execution_permissions.required_permission}</code>
        </p>
      </Section>

      <Section
        title="Approval rules"
        help="Tool calls in these side-effect classes pause for human approval. Destructive tools always require approval."
      >
        {APPROVABLE.map((c) => {
          const locked = c === REQUIRED_APPROVAL;
          return (
            <Check
              key={c}
              label={`Require approval for ${c}`}
              checked={locked || spec.approval_rules.require_approval_for.includes(c)}
              disabled={readOnly || locked}
              onChange={(on) => toggleApproval(c, on)}
              help={locked ? 'Always required; cannot be turned off.' : undefined}
            />
          );
        })}
      </Section>

      <Section
        title="Variables map"
        help="Maps prompt variables to values, one per line as name=value. Reference run input fields with {{input.field}}."
      >
        <MapField
          value={spec.variables_map}
          readOnly={readOnly}
          onChange={(m) => set({ variables_map: m })}
        />
      </Section>
    </div>
  );
}
