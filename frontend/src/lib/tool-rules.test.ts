import { describe, expect, it } from 'vitest';
import type { SideEffectClass } from './api/build-types';
import { isWriteClass, parseJsonSchemaText, retryErrors } from './tool-rules';

const spec = (
  side_effect_class: SideEffectClass,
  max_attempts: number,
  idempotency: 'none' | 'key_supported' = 'none',
) => ({ side_effect_class, retry: { max_attempts }, idempotency });

describe('retryErrors (contract amendment C1)', () => {
  it.each(['write_non_idempotent', 'destructive'] as const)(
    'blocks retry above 1 for %s even with an idempotency key',
    (c) => {
      expect(retryErrors(spec(c, 2, 'key_supported'))).toHaveLength(1);
      expect(retryErrors(spec(c, 3))[0]).toMatch(/not allowed/);
      expect(retryErrors(spec(c, 1))).toEqual([]);
    },
  );

  it('requires key_supported for write_idempotent but allows retries', () => {
    expect(retryErrors(spec('write_idempotent', 1, 'none'))[0]).toMatch(/key_supported/);
    expect(retryErrors(spec('write_idempotent', 3, 'key_supported'))).toEqual([]);
  });

  it('never restricts none/read', () => {
    expect(retryErrors(spec('none', 5))).toEqual([]);
    expect(retryErrors(spec('read', 5))).toEqual([]);
  });

  it('treats exactly the three write classes as side-effecting', () => {
    const all: SideEffectClass[] = [
      'none',
      'read',
      'write_idempotent',
      'write_non_idempotent',
      'destructive',
    ];
    expect(all.filter(isWriteClass)).toEqual([
      'write_idempotent',
      'write_non_idempotent',
      'destructive',
    ]);
  });
});

describe('parseJsonSchemaText', () => {
  it('accepts a valid object schema', () => {
    const r = parseJsonSchemaText(
      '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","properties":{"n":{"type":"integer","minimum":1}},"required":["n"]}',
    );
    expect(r.ok).toBe(true);
  });

  it('reports invalid JSON and non-objects', () => {
    expect(parseJsonSchemaText('{').ok).toBe(false);
    expect(parseJsonSchemaText('[]')).toEqual({
      ok: false,
      errors: ['The schema must be a JSON object.'],
    });
  });

  it('reports structural problems with paths', () => {
    const r = parseJsonSchemaText(
      '{"type":"strng","properties":{"a":{"type":"string","pattern":"("}},"required":"a","enum":[]}',
    );
    expect(r.ok).toBe(false);
    if (!r.ok) {
      const joined = r.errors.join('\n');
      expect(joined).toContain('$.type');
      expect(joined).toContain('$.properties.a.pattern');
      expect(joined).toContain('$.required');
      expect(joined).toContain('$.enum');
    }
  });
});
