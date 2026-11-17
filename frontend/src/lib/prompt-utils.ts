import type { PromptSpec, PromptVariable } from './api/build-types';
import type { JsonSchemaProperty } from './api/integrate-types';
import { buildConfig, validateValues, type FieldDef, type FormValues } from './schema-form';

const VAR_RE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;

export function emptyPromptSpec(): PromptSpec {
  return {
    sections: { system: '', context: '', constraints: '', output_format: '' },
    variables: [],
    examples: [],
    composed_of: [],
    evaluation_criteria: [],
  };
}

export function extractVars(text: string): string[] {
  return [...text.matchAll(VAR_RE)].map((m) => m[1] as string);
}

/** `{{x}}` placeholders used in sections that are not declared as variables (server rejects these too). */
export function undeclaredVars(spec: PromptSpec): string[] {
  const declared = new Set(spec.variables.map((v) => v.name));
  const used = new Set<string>();
  for (const text of Object.values(spec.sections))
    for (const v of extractVars(text ?? '')) used.add(v);
  return [...used].filter((v) => !declared.has(v)).sort();
}

/** One form field per declared variable (generated from the declaration, not hand-written). */
export function variableFields(vars: PromptVariable[]): FieldDef[] {
  return vars
    .filter((v) => v.name.trim() !== '')
    .map((v) => {
      const prop: JsonSchemaProperty = { type: 'string' };
      let kind: FieldDef['kind'] = 'string';
      if (v.type === 'number') {
        kind = 'number';
        prop.type = 'number';
      } else if (v.type === 'boolean') {
        kind = 'boolean';
        prop.type = 'boolean';
      }
      if (v.max_length !== undefined && kind === 'string') prop.maxLength = v.max_length;
      if (v.default !== undefined && v.default !== null)
        prop.default = typeof v.default === 'string' ? v.default : JSON.stringify(v.default);
      return {
        key: v.name,
        label: v.name,
        kind,
        required: v.required,
        description:
          v.type === 'json'
            ? 'JSON value'
            : v.trusted
              ? undefined
              : 'Untrusted: inserted as data inside an <untrusted> block.',
        prop,
      };
    });
}

export function initialVariableValues(vars: PromptVariable[]): FormValues {
  const v: FormValues = {};
  for (const f of variableFields(vars)) {
    const d = f.prop.default;
    v[f.key] = f.kind === 'boolean' ? d === 'true' || d === true : d === undefined ? '' : String(d);
  }
  return v;
}

export type VariablesResult =
  { ok: true; values: Record<string, unknown> } | { ok: false; errors: Record<string, string> };

/** Validates and converts form text into typed variable values (json variables are parsed). */
export function buildVariables(vars: PromptVariable[], form: FormValues): VariablesResult {
  const fields = variableFields(vars);
  const errors = validateValues(fields, form);
  const values = buildConfig(fields, form);
  for (const v of vars) {
    if (v.type !== 'json') continue;
    const raw = values[v.name];
    if (typeof raw !== 'string') continue;
    try {
      values[v.name] = JSON.parse(raw);
    } catch {
      errors[v.name] = 'Must be valid JSON.';
    }
  }
  return Object.keys(errors).length ? { ok: false, errors } : { ok: true, values };
}

export interface Segment {
  text: string;
  untrusted: boolean;
}

/** Splits rendered content so `<untrusted ...>...</untrusted>` data blocks can be highlighted. */
export function splitUntrusted(content: string): Segment[] {
  const out: Segment[] = [];
  const re = /<untrusted\b[^>]*>[\s\S]*?<\/untrusted>/g;
  let last = 0;
  for (const m of content.matchAll(re)) {
    const idx = m.index ?? 0;
    if (idx > last) out.push({ text: content.slice(last, idx), untrusted: false });
    out.push({ text: m[0], untrusted: true });
    last = idx + m[0].length;
  }
  if (last < content.length) out.push({ text: content.slice(last), untrusted: false });
  return out;
}
