'use client';

import type { SkillSpec } from '@/lib/api/build-types';
import { Markdown } from '@/lib/markdown';
import { SelectField, TextField } from '../admin/ui';
import type { EditorProps } from './DraftEditor';
import { VersionRefList } from './pickers';
import { Area, NumberField, Section } from './shared';

export function defaultSkillSpec(): SkillSpec {
  return {
    instructions_md: '',
    required_inputs: [],
    dependencies: [],
    applicable_tools: [],
    validation: { max_instruction_chars: 8000 },
  };
}

export function normalizeSkillSpec(raw: Record<string, unknown> | undefined): SkillSpec {
  const d = defaultSkillSpec();
  if (!raw) return d;
  const r = raw as Partial<SkillSpec>;
  return {
    ...d,
    ...r,
    required_inputs: r.required_inputs ?? [],
    dependencies: r.dependencies ?? [],
    applicable_tools: r.applicable_tools ?? [],
    validation: { ...d.validation, ...(r.validation ?? {}) },
  };
}

export const skillBlockers = (spec: SkillSpec): string[] =>
  spec.instructions_md.length > spec.validation.max_instruction_chars
    ? [
        `Instructions are ${spec.instructions_md.length} characters; the limit is ${spec.validation.max_instruction_chars}.`,
      ]
    : [];

const lines = (s: string) =>
  s
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);

export function SkillEditor({ org, spec, onChange, readOnly }: EditorProps<SkillSpec>) {
  const set = (patch: Partial<SkillSpec>) => onChange({ ...spec, ...patch });
  return (
    <div className="space-y-4">
      <Section
        title="Instructions"
        help="Markdown only. Skills are instructional text added to the agent's context; they never execute anything. Raw HTML is not rendered."
      >
        <div className="grid gap-4 lg:grid-cols-2">
          <Area
            label="Instructions (markdown)"
            value={spec.instructions_md}
            rows={14}
            readOnly={readOnly}
            monospace
            onChange={(v) => set({ instructions_md: v })}
            help={`${spec.instructions_md.length} / ${spec.validation.max_instruction_chars} characters`}
          />
          <div>
            <h4 className="label">Preview</h4>
            <div
              aria-label="Instructions preview"
              className="min-h-32 rounded-md border border-slate-200 bg-white p-3"
            >
              <Markdown source={spec.instructions_md} />
            </div>
          </div>
        </div>
        <NumberField
          label="Max instruction characters"
          min={1}
          fallback={8000}
          disabled={readOnly}
          value={spec.validation.max_instruction_chars}
          onChange={(v) => set({ validation: { max_instruction_chars: v } })}
        />
      </Section>

      <Section title="Required inputs" help="Inputs an agent must provide for this skill to apply.">
        {spec.required_inputs.map((inp, i) => (
          <div key={i} className="grid gap-2 sm:grid-cols-4">
            <TextField
              label={`Input ${i + 1} name`}
              value={inp.name}
              disabled={readOnly}
              monospace
              onChange={(name) =>
                set({
                  required_inputs: spec.required_inputs.map((x, j) =>
                    j === i ? { ...x, name } : x,
                  ),
                })
              }
            />
            <TextField
              label={`Input ${i + 1} description`}
              value={inp.description}
              disabled={readOnly}
              onChange={(description) =>
                set({
                  required_inputs: spec.required_inputs.map((x, j) =>
                    j === i ? { ...x, description } : x,
                  ),
                })
              }
            />
            <SelectField
              label={`Input ${i + 1} type`}
              value={inp.type}
              disabled={readOnly}
              onChange={(type) =>
                set({
                  required_inputs: spec.required_inputs.map((x, j) =>
                    j === i ? { ...x, type } : x,
                  ),
                })
              }
              options={['string', 'number', 'boolean', 'json'].map((t) => ({ value: t, label: t }))}
            />
            {!readOnly && (
              <div className="flex items-end">
                <button
                  type="button"
                  className="btn"
                  onClick={() =>
                    set({ required_inputs: spec.required_inputs.filter((_, j) => j !== i) })
                  }
                >
                  Remove input {i + 1}
                </button>
              </div>
            )}
          </div>
        ))}
        {!readOnly && (
          <button
            type="button"
            className="btn"
            onClick={() =>
              set({
                required_inputs: [
                  ...spec.required_inputs,
                  { name: '', description: '', type: 'string' },
                ],
              })
            }
          >
            Add input
          </button>
        )}
      </Section>

      <Section
        title="Dependencies"
        help="Published skill versions this skill builds on. Cycles are rejected at publish."
      >
        <VersionRefList
          org={org}
          kind="skills"
          label="Dependency"
          values={spec.dependencies.map((d) => d.skill_version_id)}
          readOnly={readOnly}
          onChange={(ids) => set({ dependencies: ids.map((id) => ({ skill_version_id: id })) })}
        />
      </Section>

      <Section title="Applicable tools (advisory — grants nothing)">
        <Area
          label="Applicable tool keys (one per line)"
          rows={3}
          readOnly={readOnly}
          value={spec.applicable_tools.join('\n')}
          onChange={(v) => set({ applicable_tools: lines(v) })}
          help="Advisory — grants nothing. Listing a tool here does not give an agent access to it."
        />
      </Section>
    </div>
  );
}
