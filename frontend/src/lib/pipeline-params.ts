import type { JsonSchema } from './api/integrate-types';
import {
  buildConfig,
  fieldsFromSchema,
  validateValues,
  type FieldDef,
  type FormValues,
} from './schema-form';

export interface ParamsLayout {
  fields: FieldDef[];
  /** Required/optional parameters the form cannot render (objects, arrays of objects). */
  jsonKeys: string[];
  jsonRequired: string[];
}

/** Derives the run-parameters form from `spec.params` (a JSON Schema object). */
export function paramsLayout(schema: Record<string, unknown> | undefined): ParamsLayout {
  const s = (schema ?? {}) as JsonSchema;
  const fields = fieldsFromSchema(s);
  const rendered = new Set(fields.map((f) => f.key));
  const jsonKeys = Object.keys(s.properties ?? {}).filter((k) => !rendered.has(k));
  return { fields, jsonKeys, jsonRequired: (s.required ?? []).filter((k) => jsonKeys.includes(k)) };
}

export type ParamsResult =
  | { ok: true; params: Record<string, unknown> }
  | { ok: false; errors: Record<string, string>; extraError?: string };

/** Validates the form (and the optional JSON box) and builds the `params` body. */
export function buildParams(
  layout: ParamsLayout,
  values: FormValues,
  extraText: string,
): ParamsResult {
  const errors = validateValues(layout.fields, values);
  let extra: Record<string, unknown> = {};
  let extraError: string | undefined;
  if (extraText.trim()) {
    try {
      const parsed: unknown = JSON.parse(extraText);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed))
        extraError = 'Additional parameters must be a JSON object.';
      else extra = parsed as Record<string, unknown>;
    } catch (e) {
      extraError = `Not valid JSON: ${e instanceof Error ? e.message : 'parse error'}`;
    }
  }
  if (!extraError) {
    const missing = layout.jsonRequired.filter((k) => extra[k] === undefined);
    if (missing.length) extraError = `Required: ${missing.join(', ')}.`;
  }
  if (Object.keys(errors).length || extraError) return { ok: false, errors, extraError };
  return { ok: true, params: { ...extra, ...buildConfig(layout.fields, values) } };
}
