import type { ProblemFieldError } from './api/errors';
import type { JsonSchema, JsonSchemaProperty } from './api/integrate-types';

export type FieldKind = 'string' | 'number' | 'integer' | 'boolean' | 'enum' | 'stringArray';

export interface FieldDef {
  key: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  description?: string;
  options?: (string | number)[];
  prop: JsonSchemaProperty;
}

/** Form state: text for everything except booleans. Array fields are one entry per line. */
export type FormValues = Record<string, string | boolean>;

function kindOf(p: JsonSchemaProperty): FieldKind | null {
  if (p.enum && p.enum.length > 0 && p.type !== 'array') return 'enum';
  switch (p.type) {
    case 'string':
    case undefined:
      return 'string';
    case 'number':
      return 'number';
    case 'integer':
      return 'integer';
    case 'boolean':
      return 'boolean';
    case 'array':
      return !p.items?.type || p.items.type === 'string' ? 'stringArray' : null;
    default:
      return null; // nested objects are not rendered; the server still validates
  }
}

/** Derives form fields from an adapter `config_schema`. `hidden` keys (e.g. secret fields) are skipped. */
export function fieldsFromSchema(
  schema: JsonSchema | undefined,
  hidden: readonly string[] = [],
): FieldDef[] {
  const required = new Set(schema?.required ?? []);
  const out: FieldDef[] = [];
  for (const [key, prop] of Object.entries(schema?.properties ?? {})) {
    if (hidden.includes(key)) continue;
    const kind = kindOf(prop);
    if (!kind) continue;
    out.push({
      key,
      label: prop.title ?? key,
      kind,
      required: required.has(key),
      description: prop.description,
      options: prop.enum,
      prop,
    });
  }
  return out;
}

export function initialValues(fields: FieldDef[]): FormValues {
  const v: FormValues = {};
  for (const f of fields) {
    const d = f.prop.default;
    if (f.kind === 'boolean') v[f.key] = d === true;
    else if (f.kind === 'stringArray') v[f.key] = Array.isArray(d) ? d.join('\n') : '';
    else v[f.key] = d === undefined || d === null ? '' : String(d);
  }
  return v;
}

const lines = (s: string) =>
  s
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);

/** Client-side validation mirroring the JSON-Schema keywords this form understands. */
export function validateValues(fields: FieldDef[], values: FormValues): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const f of fields) {
    const raw = values[f.key];
    const p = f.prop;
    if (f.kind === 'boolean') continue;
    const text = typeof raw === 'string' ? raw : '';
    const empty = f.kind === 'stringArray' ? lines(text).length === 0 : text.trim() === '';
    if (empty) {
      if (f.required) errors[f.key] = `${f.label} is required.`;
      continue;
    }
    if (f.kind === 'string') {
      if (p.minLength !== undefined && text.length < p.minLength)
        errors[f.key] = `Must be at least ${p.minLength} characters.`;
      else if (p.maxLength !== undefined && text.length > p.maxLength)
        errors[f.key] = `Must be at most ${p.maxLength} characters.`;
      else if (p.format === 'uri' && !/^[a-z][a-z0-9+.-]*:\/\/\S+$/i.test(text.trim()))
        errors[f.key] = 'Must be a valid URL, for example https://example.test.';
      else if (p.pattern) {
        try {
          if (!new RegExp(p.pattern).test(text)) errors[f.key] = `Must match ${p.pattern}.`;
        } catch {
          // An invalid schema pattern cannot be checked client-side; the server validates it.
        }
      }
    } else if (f.kind === 'number' || f.kind === 'integer') {
      const n = Number(text);
      if (!Number.isFinite(n)) errors[f.key] = 'Must be a number.';
      else if (f.kind === 'integer' && !Number.isInteger(n))
        errors[f.key] = 'Must be a whole number.';
      else if (p.minimum !== undefined && n < p.minimum)
        errors[f.key] = `Must be at least ${p.minimum}.`;
      else if (p.maximum !== undefined && n > p.maximum)
        errors[f.key] = `Must be at most ${p.maximum}.`;
    } else if (f.kind === 'enum') {
      if (!f.options?.map(String).includes(text))
        errors[f.key] = 'Choose one of the listed values.';
    }
  }
  return errors;
}

/** Builds the `config` object. Optional empty fields are omitted so server defaults apply. */
export function buildConfig(fields: FieldDef[], values: FormValues): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  for (const f of fields) {
    const raw = values[f.key];
    if (f.kind === 'boolean') {
      if (raw === true || f.required || f.prop.default !== undefined) config[f.key] = raw === true;
      continue;
    }
    const text = typeof raw === 'string' ? raw : '';
    if (f.kind === 'stringArray') {
      const arr = lines(text);
      if (arr.length > 0) config[f.key] = arr;
    } else if (text.trim() !== '') {
      if (f.kind === 'number' || f.kind === 'integer') config[f.key] = Number(text);
      else if (f.kind === 'enum')
        config[f.key] = f.options?.find((o) => String(o) === text) ?? text;
      else config[f.key] = text.trim();
    }
  }
  return config;
}

/** Maps server field errors (loc like [body, config, base_url]) onto form keys; the rest are returned unmatched. */
export function mapServerErrors(
  fieldErrors: ProblemFieldError[],
  keys: readonly string[],
): { byField: Record<string, string>; other: ProblemFieldError[] } {
  const byField: Record<string, string> = {};
  const other: ProblemFieldError[] = [];
  for (const e of fieldErrors) {
    const key = [...e.loc].reverse().find((p) => typeof p === 'string' && keys.includes(p));
    if (typeof key === 'string' && byField[key] === undefined) byField[key] = e.msg;
    else other.push(e);
  }
  return { byField, other };
}
