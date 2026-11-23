import { describe, expect, it } from 'vitest';
import { destinationChanged, visibleConfigFields } from './adapter-fields';
import { fieldsFromSchema } from './schema-form';

const REST = fieldsFromSchema({
  type: 'object',
  properties: {
    base_url: { type: 'string' },
    oauth2_url: { type: 'string' },
    client_id: { type: 'string' },
    scope: { type: 'string' },
    timeout_seconds: { type: 'integer' },
  },
});
const keys = (adapter: string, method: string) =>
  visibleConfigFields(adapter, method, REST).map((f) => f.key);

describe('visibleConfigFields', () => {
  it('hides the auth-only fields under any other method', () => {
    expect(keys('rest.http', 'bearer')).toEqual(['base_url', 'timeout_seconds']);
  });

  it('shows the auth-only fields under their method', () => {
    expect(keys('rest.http', 'oauth2_client_credentials')).toEqual([
      'base_url',
      'oauth2_url',
      'client_id',
      'scope',
      'timeout_seconds',
    ]);
  });

  it('hides the Databricks client_id under bearer only', () => {
    expect(keys('databricks', 'bearer')).not.toContain('client_id');
    expect(keys('databricks', 'oauth2_client_credentials')).toContain('client_id');
    expect(keys('databricks', 'bearer')).toContain('scope');
  });

  it('has no auth-only fields for an unknown adapter', () => {
    expect(keys('unknown', 'none')).toHaveLength(REST.length);
  });
});

describe('destinationChanged', () => {
  it('is true when a destination field changes', () => {
    expect(destinationChanged('databricks', { host: 'https://a' }, { host: 'https://b' })).toBe(
      true,
    );
    expect(destinationChanged('postgres', { port: 5432 }, { port: 5433 })).toBe(true);
    expect(destinationChanged('rest.http', {}, { oauth2_url: 'https://t' })).toBe(true);
  });

  it('is false when only other fields change', () => {
    expect(
      destinationChanged('databricks', { host: 'h', max_rows: 1 }, { host: 'h', max_rows: 2 }),
    ).toBe(false);
  });

  it('is false for an unknown adapter', () => {
    expect(destinationChanged('unknown', { host: 'a' }, { host: 'b' })).toBe(false);
  });
});
