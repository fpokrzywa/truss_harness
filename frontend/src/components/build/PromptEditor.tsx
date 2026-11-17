'use client';

import { useState } from 'react';
import type {
  CriterionType,
  EvaluationCriterion,
  PromptComposition,
  PromptExample,
  PromptSpec,
  PromptVariable,
  VariableType,
} from '@/lib/api/build-types';
import { emptyPromptSpec, undeclaredVars } from '@/lib/prompt-utils';
import { SelectField, TextField } from '../admin/ui';
import type { EditorProps } from './DraftEditor';
import { ModelSelect, VersionPicker } from './pickers';
import { Area, Check, JsonField, num, Section } from './shared';

/** Merges a stored spec over the defaults so every editor field exists. */
export function normalizePromptSpec(raw: Record<string, unknown> | undefined): PromptSpec {
  const d = emptyPromptSpec();
  if (!raw) return d;
  const r = raw as Partial<PromptSpec>;
  return {
    ...d,
    ...r,
    sections: { ...d.sections, ...(r.sections ?? {}) },
    variables: r.variables ?? [],
    examples: r.examples ?? [],
    composed_of: r.composed_of ?? [],
    evaluation_criteria: r.evaluation_criteria ?? [],
  };
}

const VAR_TYPES: VariableType[] = ['string', 'text', 'number', 'boolean', 'json'];
const CRITERIA: CriterionType[] = [
  'contains',
  'not_contains',
  'regex',
  'json_schema',
  'max_length',
];

function replaceAt<T>(list: T[], i: number, item: T): T[] {
  return list.map((x, j) => (j === i ? item : x));
}

export function PromptEditor({
  org,
  spec,
  onChange,
  readOnly,
  reportInvalid,
}: EditorProps<PromptSpec>) {
  // Rows hold uncommitted JSON text; bump the generation on removal so text never shifts between rows.
  const [gen, setGen] = useState(0);
  const set = (patch: Partial<PromptSpec>) => onChange({ ...spec, ...patch });
  const section = (key: keyof PromptSpec['sections'], v: string) =>
    set({ sections: { ...spec.sections, [key]: v } });
  const undeclared = undeclaredVars(spec);

  return (
    <div className="space-y-4">
      <Section
        title="Sections"
        help="Use {{variable}} placeholders only (no logic or code). The system section becomes the system message; context, constraints and output format form the instruction message."
      >
        <Area
          label="System"
          value={spec.sections.system}
          rows={5}
          readOnly={readOnly}
          onChange={(v) => section('system', v)}
        />
        <Area
          label="Context"
          value={spec.sections.context ?? ''}
          readOnly={readOnly}
          onChange={(v) => section('context', v)}
        />
        <Area
          label="Constraints"
          value={spec.sections.constraints ?? ''}
          readOnly={readOnly}
          onChange={(v) => section('constraints', v)}
        />
        <Area
          label="Output format"
          value={spec.sections.output_format ?? ''}
          readOnly={readOnly}
          onChange={(v) => section('output_format', v)}
        />
        {undeclared.length > 0 && (
          <p role="alert" className="text-sm text-red-700">
            Undeclared variables used in sections: {undeclared.map((v) => `{{${v}}}`).join(', ')}.
            Declare them below; the server rejects undeclared variables.
          </p>
        )}
      </Section>

      <Section
        title="Variables"
        help="Untrusted variables (the default) are inserted only inside <untrusted> data blocks, escaped so their content cannot close the block or be read as instructions."
      >
        {spec.variables.map((v, i) => (
          <VariableRow
            key={`${gen}-${i}`}
            index={i}
            variable={v}
            readOnly={readOnly}
            onChange={(nv) => set({ variables: replaceAt(spec.variables, i, nv) })}
            onRemove={() => {
              setGen((g) => g + 1);
              set({ variables: spec.variables.filter((_, j) => j !== i) });
            }}
            reportInvalid={reportInvalid}
          />
        ))}
        {!readOnly && (
          <button
            type="button"
            className="btn"
            onClick={() =>
              set({
                variables: [
                  ...spec.variables,
                  { name: '', type: 'string', required: true, trusted: false },
                ],
              })
            }
          >
            Add variable
          </button>
        )}
      </Section>

      <Section title="Examples" help="Sample inputs and the expected output.">
        {spec.examples.map((ex, i) => (
          <ExampleRow
            key={`${gen}-${i}`}
            index={i}
            example={ex}
            readOnly={readOnly}
            onChange={(n) => set({ examples: replaceAt(spec.examples, i, n) })}
            onRemove={() => {
              setGen((g) => g + 1);
              set({ examples: spec.examples.filter((_, j) => j !== i) });
            }}
            reportInvalid={reportInvalid}
          />
        ))}
        {!readOnly && (
          <button
            type="button"
            className="btn"
            onClick={() => set({ examples: [...spec.examples, { inputs: {}, output: '' }] })}
          >
            Add example
          </button>
        )}
      </Section>

      <Section
        title="Composition"
        help="Include published prompt versions. Composed system parts become part of the system message."
      >
        {spec.composed_of.map((c: PromptComposition, i) => (
          <div key={i} className="space-y-2 rounded-md border border-slate-200 p-2">
            <VersionPicker
              org={org}
              kind="prompts"
              label={`Included prompt ${i + 1}`}
              value={c.prompt_version_id}
              disabled={readOnly}
              onChange={(v) =>
                set({ composed_of: replaceAt(spec.composed_of, i, { ...c, prompt_version_id: v }) })
              }
            />
            <SelectField
              label={`Placement ${i + 1}`}
              value={c.placement}
              disabled={readOnly}
              onChange={(v) =>
                set({
                  composed_of: replaceAt(spec.composed_of, i, {
                    ...c,
                    placement: v as PromptComposition['placement'],
                  }),
                })
              }
              options={[
                { value: 'system_prefix', label: 'System prefix' },
                { value: 'context_prefix', label: 'Context prefix' },
              ]}
            />
            {!readOnly && (
              <button
                type="button"
                className="btn"
                onClick={() => set({ composed_of: spec.composed_of.filter((_, j) => j !== i) })}
              >
                Remove included prompt {i + 1}
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
                composed_of: [
                  ...spec.composed_of,
                  { prompt_version_id: '', placement: 'system_prefix' },
                ],
              })
            }
          >
            Add included prompt
          </button>
        )}
      </Section>

      <Section title="Default model">
        <ModelSelect
          org={org}
          label="Default model"
          value={spec.default_model_id ?? ''}
          disabled={readOnly}
          emptyLabel="No default"
          onChange={(id) => set({ default_model_id: id || undefined })}
        />
      </Section>

      <Section
        title="Evaluation criteria"
        help="Checked against the model output when the prompt is tested."
      >
        {spec.evaluation_criteria.map((c: EvaluationCriterion, i) => (
          <div key={i} className="grid gap-2 sm:grid-cols-4">
            <TextField
              label={`Criterion ${i + 1} name`}
              value={c.name}
              disabled={readOnly}
              onChange={(v) =>
                set({
                  evaluation_criteria: replaceAt(spec.evaluation_criteria, i, { ...c, name: v }),
                })
              }
            />
            <SelectField
              label={`Criterion ${i + 1} type`}
              value={c.type}
              disabled={readOnly}
              onChange={(v) =>
                set({
                  evaluation_criteria: replaceAt(spec.evaluation_criteria, i, {
                    ...c,
                    type: v as CriterionType,
                    value: v === 'max_length' ? 0 : '',
                  }),
                })
              }
              options={CRITERIA.map((t) => ({ value: t, label: t }))}
            />
            <TextField
              label={`Criterion ${i + 1} value`}
              value={String(c.value)}
              disabled={readOnly}
              type={c.type === 'max_length' ? 'number' : 'text'}
              onChange={(v) =>
                set({
                  evaluation_criteria: replaceAt(spec.evaluation_criteria, i, {
                    ...c,
                    value: c.type === 'max_length' ? (num(v) ?? 0) : v,
                  }),
                })
              }
            />
            {!readOnly && (
              <div className="flex items-end">
                <button
                  type="button"
                  className="btn"
                  onClick={() =>
                    set({ evaluation_criteria: spec.evaluation_criteria.filter((_, j) => j !== i) })
                  }
                >
                  Remove criterion {i + 1}
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
                evaluation_criteria: [
                  ...spec.evaluation_criteria,
                  { type: 'contains', value: '', name: '' },
                ],
              })
            }
          >
            Add criterion
          </button>
        )}
      </Section>
    </div>
  );
}

function VariableRow({
  index,
  variable: v,
  readOnly,
  onChange,
  onRemove,
  reportInvalid,
}: {
  index: number;
  variable: PromptVariable;
  readOnly: boolean;
  onChange: (v: PromptVariable) => void;
  onRemove: () => void;
  reportInvalid: EditorProps<PromptSpec>['reportInvalid'];
}) {
  const n = index + 1;
  return (
    <div className="space-y-2 rounded-md border border-slate-200 p-3">
      <div className="grid gap-2 sm:grid-cols-4">
        <TextField
          label={`Variable ${n} name`}
          value={v.name}
          disabled={readOnly}
          monospace
          onChange={(name) => onChange({ ...v, name })}
          error={
            v.name && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(v.name)
              ? 'Letters, digits and underscore only.'
              : null
          }
        />
        <SelectField
          label={`Variable ${n} type`}
          value={v.type}
          disabled={readOnly}
          onChange={(t) => onChange({ ...v, type: t as VariableType })}
          options={VAR_TYPES.map((t) => ({ value: t, label: t }))}
        />
        <TextField
          label={`Variable ${n} default`}
          value={
            v.default === undefined || v.default === null
              ? ''
              : typeof v.default === 'string'
                ? v.default
                : JSON.stringify(v.default)
          }
          disabled={readOnly}
          onChange={(d) => onChange({ ...v, default: d === '' ? undefined : d })}
        />
        <TextField
          label={`Variable ${n} max length`}
          value={v.max_length === undefined ? '' : String(v.max_length)}
          type="number"
          disabled={readOnly}
          onChange={(m) => onChange({ ...v, max_length: num(m) })}
        />
      </div>
      <div className="flex flex-wrap gap-6">
        <Check
          label={`Variable ${n} required`}
          checked={v.required}
          disabled={readOnly}
          onChange={(required) => onChange({ ...v, required })}
        />
        <Check
          label={`Variable ${n} trusted`}
          checked={v.trusted}
          disabled={readOnly}
          onChange={(trusted) => onChange({ ...v, trusted })}
          help={
            v.trusted
              ? 'Trusted: inserted as plain template text. Only enable for values authored by your team.'
              : 'Untrusted (default): wrapped as data in an <untrusted> block, never as instructions.'
          }
        />
      </div>
      {v.type === 'json' && (
        <JsonField
          label={`Variable ${n} JSON schema`}
          value={v.json_schema}
          readOnly={readOnly}
          rows={4}
          onChange={(json_schema) => onChange({ ...v, json_schema })}
          onInvalid={(m) => reportInvalid(`var-${index}-schema`, m)}
        />
      )}
      {!readOnly && (
        <button type="button" className="btn" onClick={onRemove}>
          Remove variable {n}
        </button>
      )}
    </div>
  );
}

function ExampleRow({
  index,
  example,
  readOnly,
  onChange,
  onRemove,
  reportInvalid,
}: {
  index: number;
  example: PromptExample;
  readOnly: boolean;
  onChange: (e: PromptExample) => void;
  onRemove: () => void;
  reportInvalid: EditorProps<PromptSpec>['reportInvalid'];
}) {
  const n = index + 1;
  return (
    <div className="space-y-2 rounded-md border border-slate-200 p-3">
      <JsonField
        label={`Example ${n} inputs (JSON object)`}
        value={example.inputs}
        readOnly={readOnly}
        rows={3}
        check={(p) =>
          typeof p === 'object' && p !== null && !Array.isArray(p)
            ? []
            : ['Inputs must be a JSON object.']
        }
        onChange={(inputs) =>
          onChange({ ...example, inputs: (inputs ?? {}) as Record<string, unknown> })
        }
        onInvalid={(m) => reportInvalid(`example-${index}`, m)}
      />
      <Area
        label={`Example ${n} output`}
        value={example.output}
        readOnly={readOnly}
        rows={3}
        onChange={(output) => onChange({ ...example, output })}
      />
      {!readOnly && (
        <button type="button" className="btn" onClick={onRemove}>
          Remove example {n}
        </button>
      )}
    </div>
  );
}
