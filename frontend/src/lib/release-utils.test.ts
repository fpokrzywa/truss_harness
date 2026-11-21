import { describe, expect, it } from 'vitest';
import { ApiError } from './api/errors';
import { compatibilityReport, shortDigest, validateCases } from './release-utils';

const assertion = (over: Record<string, unknown> = {}) => ({
  id: 'a1',
  kind: 'contains',
  value: 'hello',
  ...over,
});
const kase = (over: Record<string, unknown> = {}) => ({
  case_id: 'c1',
  input: { message: 'hi' },
  assertions: [assertion()],
  ...over,
});
const errorOf = (value: unknown) => {
  const r = validateCases(typeof value === 'string' ? value : JSON.stringify(value));
  if (!('error' in r)) throw new Error('expected an error');
  return r.error;
};

describe('validateCases', () => {
  it('returns the cases of a valid two-case input', () => {
    const cases = [
      kase(),
      kase({ case_id: 'c2', assertions: [assertion({ kind: 'regex', value: 'h.*' })] }),
    ];
    expect(validateCases(JSON.stringify(cases, null, 2))).toEqual({ cases });
  });

  it('returns a distinct error for each problem', () => {
    const errors = [
      errorOf('{not json'),
      errorOf({ case_id: 'c1' }),
      errorOf([]),
      errorOf(Array.from({ length: 201 }, (_, i) => kase({ case_id: `c${i}` }))),
      errorOf([kase({ case_id: '' })]),
      errorOf([kase({ input: 'hi' })]),
      errorOf([kase(), kase()]),
      errorOf([kase({ assertions: [] })]),
      errorOf([
        kase({ assertions: Array.from({ length: 51 }, (_, i) => assertion({ id: `a${i}` })) }),
      ]),
      errorOf([kase({ assertions: [assertion({ id: '' })] })]),
      errorOf([kase({ assertions: [assertion(), assertion()] })]),
      errorOf([kase({ assertions: [assertion({ kind: 'startswith' })] })]),
      errorOf([kase({ assertions: [assertion({ kind: 'regex', value: 'x'.repeat(201) })] })]),
      errorOf([kase({ input: { message: 'é'.repeat(600_000) } })]),
    ];
    expect(errors[0]).toBe('The cases are not valid JSON.');
    expect(errors[1]).toBe('The cases must be a JSON array.');
    expect(errors[2]).toBe('Add at least one case.');
    expect(errors[3]).toMatch(/at most 200 cases/);
    expect(errors[4]).toMatch(/needs a case_id/);
    expect(errors[5]).toMatch(/needs an input object/);
    expect(errors[6]).toBe('Duplicate case_id "c1".');
    expect(errors[7]).toMatch(/at least one assertion/);
    expect(errors[8]).toMatch(/more than 50 assertions/);
    expect(errors[9]).toMatch(/assertion without an id/);
    expect(errors[10]).toMatch(/duplicate assertion id "a1"/);
    expect(errors[11]).toMatch(/unknown kind/);
    expect(errors[12]).toMatch(/regex is at most 200 characters/);
    expect(errors[13]).toBe('The cases exceed 1048576 bytes.');
    expect(new Set(errors).size).toBe(errors.length);
  });

  it('measures the size in UTF-8 bytes, not characters', () => {
    // 600,000 two-byte characters: 1.2 MB, though fewer than 1,048,576 characters.
    expect(errorOf([kase({ input: { message: 'é'.repeat(600_000) } })])).toMatch(/exceed/);
    expect(
      'cases' in validateCases(JSON.stringify([kase({ input: { m: 'a'.repeat(600_000) } })])),
    ).toBe(true);
  });
});

describe('compatibilityReport', () => {
  it('returns null for any other error', () => {
    expect(compatibilityReport(new Error('boom'))).toBeNull();
    expect(compatibilityReport(null)).toBeNull();
    expect(
      compatibilityReport(
        new ApiError({
          status: 422,
          code: 'validation_failed',
          fieldErrors: [{ loc: ['compatibility', 'entrypoint'], msg: 'x', type: 'unsupported' }],
        }),
      ),
    ).toBeNull();
  });

  it('rebuilds the diagnostics of a refused seal, rejoining pin names that contain dots', () => {
    const error = new ApiError({
      status: 422,
      code: 'release_incompatible',
      fieldErrors: [
        { loc: ['compatibility', 'entrypoint'], msg: 'supported', type: 'supported' },
        { loc: ['compatibility', 'tools', 't1'], msg: 'writes', type: 'unsupported' },
        {
          loc: ['compatibility', 'models', 'gpt-4.1', 'mini'],
          msg: 'no model binding',
          type: 'unsupported',
        },
        { loc: ['body', 'other'], msg: 'ignored', type: 'value_error' },
      ],
    });
    expect(compatibilityReport(error)).toEqual([
      { path: 'entrypoint', status: 'supported', reason: null },
      { path: 'tools.t1', status: 'unsupported', reason: 'writes' },
      { path: 'models.gpt-4.1.mini', status: 'unsupported', reason: 'no model binding' },
    ]);
  });
});

describe('shortDigest', () => {
  it('keeps the first 12 characters', () => {
    expect(shortDigest('0123456789abcdef')).toBe('0123456789ab');
  });
});
