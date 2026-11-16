import { describe, expect, it } from 'vitest';
import { ApiError } from './api/errors';
import { ADAPTER_REST } from '@/test/integrate';
import {
  buildConfig,
  fieldsFromSchema,
  initialValues,
  mapServerErrors,
  validateValues,
} from './schema-form';

const fields = fieldsFromSchema(ADAPTER_REST.config_schema, ADAPTER_REST.secret_fields);

describe('schema-driven form', () => {
  it('derives typed fields, marks required, and hides secret fields', () => {
    expect(fields.map((f) => [f.key, f.kind, f.required])).toEqual([
      ['base_url', 'string', true],
      ['timeout_seconds', 'integer', false],
      ['verify_tls', 'boolean', false],
      ['allowed_methods', 'stringArray', false],
      ['auth_style', 'enum', false],
    ]);
    expect(fields.some((f) => f.key === 'api_token')).toBe(false);
  });

  it('skips unsupported nested object properties instead of rendering them wrongly', () => {
    const f = fieldsFromSchema({
      properties: { nested: { type: 'object' }, ok: { type: 'string' } },
    });
    expect(f.map((x) => x.key)).toEqual(['ok']);
  });

  it('seeds defaults', () => {
    expect(initialValues(fields).verify_tls).toBe(true);
    expect(initialValues(fields).base_url).toBe('');
  });

  it('mirrors schema validation: required, format, bounds, integer, enum', () => {
    const v = initialValues(fields);
    expect(validateValues(fields, v).base_url).toBe('Base URL is required.');
    expect(validateValues(fields, { ...v, base_url: 'nope' }).base_url).toMatch(/valid URL/);
    expect(validateValues(fields, { ...v, base_url: 'https://a.test' })).toEqual({});
    const ok = { ...v, base_url: 'https://a.test' };
    expect(validateValues(fields, { ...ok, timeout_seconds: '0' }).timeout_seconds).toMatch(
      /at least 1/,
    );
    expect(validateValues(fields, { ...ok, timeout_seconds: '61' }).timeout_seconds).toMatch(
      /at most 60/,
    );
    expect(validateValues(fields, { ...ok, timeout_seconds: '2.5' }).timeout_seconds).toMatch(
      /whole/,
    );
    expect(validateValues(fields, { ...ok, timeout_seconds: 'abc' }).timeout_seconds).toMatch(
      /number/,
    );
    expect(validateValues(fields, { ...ok, auth_style: 'cookie' }).auth_style).toMatch(/listed/);
  });

  it('builds a typed config and omits empty optional values', () => {
    const v = {
      ...initialValues(fields),
      base_url: ' https://a.test ',
      timeout_seconds: '30',
      allowed_methods: 'GET\n\n POST ',
    };
    expect(buildConfig(fields, v)).toEqual({
      base_url: 'https://a.test',
      timeout_seconds: 30,
      verify_tls: true,
      allowed_methods: ['GET', 'POST'],
    });
  });

  it('maps server field errors to form keys and returns the rest', () => {
    const err = new ApiError({
      status: 422,
      fieldErrors: [
        { loc: ['body', 'config', 'base_url'], msg: 'host not allowed' },
        { loc: ['body', 'extra'], msg: 'other' },
      ],
    });
    const { byField, other } = mapServerErrors(err.fieldErrors, ['base_url']);
    expect(byField).toEqual({ base_url: 'host not allowed' });
    expect(other).toHaveLength(1);
  });
});
