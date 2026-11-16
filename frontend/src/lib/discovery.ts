import type { Discovery } from './api/integrate-types';

export interface DiscoveredPath {
  path: string;
  methods: string[];
}
export interface DiscoveredTool {
  name: string;
  description?: string;
}
export interface DiscoveredTable {
  schema?: string;
  name: string;
  columns: { name: string; type?: string }[];
}
export interface NormalizedDiscovery {
  paths: DiscoveredPath[];
  tools: DiscoveredTool[];
  tables: DiscoveredTable[];
}

const HTTP = new Set(['get', 'put', 'post', 'delete', 'patch', 'head', 'options']);

const isRec = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined);

/**
 * The contract names three stored-discovery kinds (openapi paths, mcp_tools, schema tables+columns)
 * but not the field layout, so this tolerates the payload at the top level or under `result`/`data`.
 */
export function normalizeDiscovery(d: Discovery): NormalizedDiscovery {
  const base: Record<string, unknown> = isRec(d.payload)
    ? d.payload
    : isRec(d.result)
      ? d.result
      : isRec(d.data)
        ? d.data
        : { ...d };
  const out: NormalizedDiscovery = { paths: [], tools: [], tables: [] };

  const paths = base.paths;
  if (Array.isArray(paths)) {
    for (const p of paths) {
      if (typeof p === 'string') out.paths.push({ path: p, methods: [] });
      else if (isRec(p) && str(p.path)) {
        const m = Array.isArray(p.methods)
          ? p.methods.filter((x): x is string => typeof x === 'string')
          : [];
        const single = str(p.method);
        out.paths.push({ path: p.path as string, methods: single ? [single, ...m] : m });
      }
    }
  } else if (isRec(paths)) {
    for (const [path, ops] of Object.entries(paths)) {
      const methods = isRec(ops) ? Object.keys(ops).filter((k) => HTTP.has(k.toLowerCase())) : [];
      out.paths.push({ path, methods: methods.map((m) => m.toUpperCase()) });
    }
  }

  if (Array.isArray(base.tools)) {
    for (const t of base.tools) {
      if (typeof t === 'string') out.tools.push({ name: t });
      else if (isRec(t) && str(t.name))
        out.tools.push({ name: t.name as string, description: str(t.description) });
    }
  }

  if (Array.isArray(base.tables)) {
    for (const t of base.tables) {
      if (!isRec(t) || !str(t.name)) continue;
      const cols = Array.isArray(t.columns) ? t.columns : [];
      out.tables.push({
        schema: str(t.schema),
        name: t.name as string,
        columns: cols.flatMap((c) =>
          typeof c === 'string'
            ? [{ name: c }]
            : isRec(c) && str(c.name)
              ? [{ name: c.name as string, type: str(c.type) ?? str(c.data_type) }]
              : [],
        ),
      });
    }
  }
  return out;
}
