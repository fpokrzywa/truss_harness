import { describe, expect, it } from 'vitest';
import { buildParams, paramsLayout } from './pipeline-params';
import { initialValues } from './schema-form';

const schema = {
  type: 'object',
  required: ['region', 'filters'],
  properties: {
    region: { type: 'string', title: 'Region' },
    limit: { type: 'integer', minimum: 1, maximum: 100 },
    dry_run: { type: 'boolean' },
    filters: { type: 'object' },
  },
};

describe('pipeline params form', () => {
  const layout = paramsLayout(schema);

  it('renders scalar params as fields and objects through the JSON box', () => {
    expect(layout.fields.map((f) => f.key)).toEqual(['region', 'limit', 'dry_run']);
    expect(layout.jsonKeys).toEqual(['filters']);
    expect(layout.jsonRequired).toEqual(['filters']);
  });

  it('requires required params and range-checks numbers', () => {
    const v = { ...initialValues(layout.fields), limit: '500' };
    const r = buildParams(layout, v, '');
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.errors.region).toBe('Region is required.');
      expect(r.errors.limit).toBe('Must be at most 100.');
      expect(r.extraError).toBe('Required: filters.');
    }
  });

  it('rejects invalid or non-object JSON in the additional parameters box', () => {
    const v = { ...initialValues(layout.fields), region: 'eu' };
    const bad = buildParams(layout, v, '{oops');
    expect(bad.ok === false && bad.extraError).toMatch(/Not valid JSON/);
    const arr = buildParams(layout, v, '[1]');
    expect(arr.ok === false && arr.extraError).toMatch(/JSON object/);
  });

  it('builds typed params', () => {
    const v = { ...initialValues(layout.fields), region: 'eu', limit: '10', dry_run: true };
    const r = buildParams(layout, v, '{"filters": {"open": true}}');
    expect(r).toEqual({
      ok: true,
      params: { filters: { open: true }, region: 'eu', limit: 10, dry_run: true },
    });
  });

  it('handles a pipeline without params', () => {
    const none = paramsLayout(undefined);
    expect(buildParams(none, {}, '')).toEqual({ ok: true, params: {} });
  });
});
