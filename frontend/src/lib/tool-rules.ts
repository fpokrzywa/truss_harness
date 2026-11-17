import type { Idempotency, SideEffectClass } from './api/build-types';

export const SIDE_EFFECT_CLASSES: SideEffectClass[] = [
  'none',
  'read',
  'write_idempotent',
  'write_non_idempotent',
  'destructive',
];

/** Classes that change external state: test runs need acknowledgement (+ tool:test_side_effects). */
export const WRITE_CLASSES: readonly SideEffectClass[] = [
  'write_idempotent',
  'write_non_idempotent',
  'destructive',
];

export const isWriteClass = (c: SideEffectClass): boolean => WRITE_CLASSES.includes(c);

export const SIDE_EFFECT_WARNINGS: Record<SideEffectClass, string> = {
  none: 'Pure computation; touches nothing outside this platform.',
  read: 'Reads external data; no state is changed.',
  write_idempotent:
    'Changes external state, but repeating a call has the same effect. Requires an idempotency key to be supported. Test runs perform real writes.',
  write_non_idempotent:
    'Changes external state and repeating a call repeats the effect (for example sending a message or email). Retries are not allowed and agents require approval by default. Test runs perform real writes.',
  destructive:
    'Deletes or irreversibly changes external state. Retries are not allowed and agents always require approval. Test runs perform real, irreversible changes.',
};

/** Mirrors server publish validation (contract section 3, amendment C1). Returns blocking messages. */
export function retryErrors(spec: {
  side_effect_class: SideEffectClass;
  retry: { max_attempts: number };
  idempotency: Idempotency;
}): string[] {
  const errors: string[] = [];
  const c = spec.side_effect_class;
  if (spec.retry.max_attempts > 1 && (c === 'write_non_idempotent' || c === 'destructive')) {
    errors.push(
      `Retries above 1 are not allowed for ${c} tools: a repeated call would repeat the side effect.`,
    );
  }
  if (c === 'write_idempotent' && spec.idempotency !== 'key_supported') {
    errors.push('write_idempotent tools require idempotency "key_supported".');
  }
  return errors;
}

const TYPES = ['string', 'number', 'integer', 'boolean', 'array', 'object', 'null'];
const NUM_KEYS = [
  'minimum',
  'maximum',
  'exclusiveMinimum',
  'exclusiveMaximum',
  'minLength',
  'maxLength',
  'minItems',
  'maxItems',
  'minProperties',
  'maxProperties',
  'multipleOf',
];

const isRec = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

function checkSchema(node: unknown, path: string, errors: string[]): void {
  if (typeof node === 'boolean') return;
  if (!isRec(node)) {
    errors.push(`${path}: a schema must be an object or boolean.`);
    return;
  }
  const t = node.type;
  if (t !== undefined) {
    const list = Array.isArray(t) ? t : [t];
    if (list.length === 0 || list.some((x) => typeof x !== 'string' || !TYPES.includes(x))) {
      errors.push(`${path}.type: must be one of ${TYPES.join(', ')}.`);
    }
  }
  if (node.properties !== undefined) {
    if (!isRec(node.properties)) errors.push(`${path}.properties: must be an object.`);
    else
      for (const [k, v] of Object.entries(node.properties))
        checkSchema(v, `${path}.properties.${k}`, errors);
  }
  if (node.required !== undefined) {
    if (!Array.isArray(node.required) || node.required.some((x) => typeof x !== 'string'))
      errors.push(`${path}.required: must be an array of strings.`);
  }
  if (node.items !== undefined) checkSchema(node.items, `${path}.items`, errors);
  if (node.additionalProperties !== undefined)
    checkSchema(node.additionalProperties, `${path}.additionalProperties`, errors);
  if (node.enum !== undefined && (!Array.isArray(node.enum) || node.enum.length === 0))
    errors.push(`${path}.enum: must be a non-empty array.`);
  for (const key of ['allOf', 'anyOf', 'oneOf'] as const) {
    const v = node[key];
    if (v === undefined) continue;
    if (!Array.isArray(v) || v.length === 0)
      errors.push(`${path}.${key}: must be a non-empty array.`);
    else v.forEach((s, i) => checkSchema(s, `${path}.${key}[${i}]`, errors));
  }
  for (const key of NUM_KEYS) {
    if (node[key] !== undefined && typeof node[key] !== 'number')
      errors.push(`${path}.${key}: must be a number.`);
  }
  if (node.pattern !== undefined) {
    if (typeof node.pattern !== 'string') errors.push(`${path}.pattern: must be a string.`);
    else {
      try {
        new RegExp(node.pattern);
      } catch {
        errors.push(`${path}.pattern: not a valid regular expression.`);
      }
    }
  }
  if (node.$schema !== undefined && typeof node.$schema !== 'string')
    errors.push(`${path}.$schema: must be a string.`);
}

/** Structural problems of an already-parsed JSON Schema value. */
export function schemaErrors(value: unknown): string[] {
  if (!isRec(value)) return ['The schema must be a JSON object.'];
  const errors: string[] = [];
  checkSchema(value, '$', errors);
  return errors;
}

export type SchemaParse =
  { ok: true; value: Record<string, unknown> } | { ok: false; errors: string[] };

/** Parses a JSON Schema textarea and applies basic draft 2020-12 structural checks (server is authoritative). */
export function parseJsonSchemaText(text: string): SchemaParse {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (e) {
    return {
      ok: false,
      errors: [`Not valid JSON: ${e instanceof Error ? e.message : 'parse error'}`],
    };
  }
  const errors = schemaErrors(value);
  return errors.length || !isRec(value) ? { ok: false, errors } : { ok: true, value };
}
