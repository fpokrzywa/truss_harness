import { describe, expect, it } from 'vitest';
import { normalizeDiscovery } from './discovery';

describe('normalizeDiscovery', () => {
  it('reads OpenAPI paths objects into methods', () => {
    const d = normalizeDiscovery({
      kind: 'openapi',
      result: { paths: { '/items': { get: {}, post: {}, parameters: [] } } },
    });
    expect(d.paths).toEqual([{ path: '/items', methods: ['GET', 'POST'] }]);
  });

  it('reads MCP tools and database tables with columns', () => {
    const d = normalizeDiscovery({
      tools: [{ name: 'echo', description: 'Echo text' }, 'ping'],
      tables: [
        { schema: 'public', name: 'customers', columns: [{ name: 'id', type: 'uuid' }, 'email'] },
      ],
    });
    expect(d.tools).toEqual([{ name: 'echo', description: 'Echo text' }, { name: 'ping' }]);
    expect(d.tables[0]?.columns).toEqual([{ name: 'id', type: 'uuid' }, { name: 'email' }]);
  });

  it('tolerates empty or unknown payloads', () => {
    expect(normalizeDiscovery({})).toEqual({ paths: [], tools: [], tables: [], truncated: false });
  });
});

describe('normalizeDiscovery (B4 payload shape)', () => {
  it('reads tables from the payload field returned by the backend', () => {
    const out = normalizeDiscovery({
      id: 'd1',
      kind: 'schema',
      discovered_at: '2026-11-04T00:00:00Z',
      payload: {
        tables: [
          {
            schema: 'public',
            name: 'customers',
            columns: [{ name: 'id', type: 'integer', nullable: false }],
          },
        ],
      },
    });
    expect(out.tables.map((t) => t.name)).toEqual(['customers']);
    expect(out.tables[0]?.columns[0]?.name).toBe('id');
  });
});

describe('normalizeDiscovery (CM-7 catalogs)', () => {
  it('keeps the catalog of each table and the truncated flag', () => {
    const out = normalizeDiscovery({
      payload: {
        tables: [
          { catalog: 'main', schema: 'sales', name: 'orders', columns: [{ name: 'id' }] },
          { schema: 'public', name: 'plain', columns: [] },
        ],
        truncated: true,
      },
    });
    expect(out.tables.map((t) => t.catalog)).toEqual(['main', undefined]);
    expect(out.truncated).toBe(true);
  });

  it('is truncated only when the payload says exactly true', () => {
    expect(normalizeDiscovery({ payload: { tables: [], truncated: 'yes' } }).truncated).toBe(false);
    expect(normalizeDiscovery({ payload: { tables: [] } }).truncated).toBe(false);
  });
});
