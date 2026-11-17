import type { ConnectionType, JsonSchema } from './api/integrate-types';
import type { ArtifactKind } from './api/build-types';
import type { PipelineNode } from './api/pipeline-types';
import { fieldsFromSchema, initialValues, type FieldDef, type FormValues } from './schema-form';

/** Config keys rendered with resource pickers instead of plain inputs. */
export type ResourceKey =
  'connection_id' | 'agent_version_id' | 'prompt_version_id' | 'tool_version_id' | 'model_id';

export const VERSION_KEYS: Record<
  'agent_version_id' | 'prompt_version_id' | 'tool_version_id',
  { kind: ArtifactKind; label: string }
> = {
  agent_version_id: { kind: 'agents', label: 'Agent' },
  prompt_version_id: { kind: 'prompts', label: 'Prompt' },
  tool_version_id: { kind: 'tools', label: 'Tool' },
};

const NODE_RESOURCES: Record<string, ResourceKey[]> = {
  data_source: ['connection_id'],
  db_query: ['connection_id'],
  api_request: ['connection_id'],
  agent: ['agent_version_id'],
  prompt: ['prompt_version_id'],
  tool: ['tool_version_id'],
  model: ['model_id'],
};

/** Connection types a node's connection picker offers. */
export function connectionTypesFor(nodeType: string): ConnectionType[] {
  if (nodeType === 'api_request') return ['rest'];
  if (nodeType === 'db_query' || nodeType === 'data_source') return ['database', 'data_platform'];
  return ['rest', 'database', 'data_platform'];
}

const RESOURCE_KEYS: readonly string[] = [
  'connection_id',
  'agent_version_id',
  'prompt_version_id',
  'tool_version_id',
  'model_id',
];

/** Resource pickers to show: implied by the node type, plus any schema property with a resource name. */
export function resourceKeysFor(node: PipelineNode, schema: JsonSchema | undefined): ResourceKey[] {
  const keys = new Set<string>(NODE_RESOURCES[node.type] ?? []);
  for (const k of Object.keys(schema?.properties ?? {})) if (RESOURCE_KEYS.includes(k)) keys.add(k);
  // A sink only needs a connection when it targets one.
  if (node.type === 'sink' && node.config.target === 'connection') keys.add('connection_id');
  return RESOURCE_KEYS.filter((k) => keys.has(k)) as ResourceKey[];
}

/** Sink keys that only apply when the sink delivers to a connection (the server rejects them otherwise). */
const SINK_CONNECTION_KEYS = ['connection_id', 'method', 'path_template'] as const;

const isConnectionSink = (node: PipelineNode): boolean =>
  node.type === 'sink' && node.config.target === 'connection';

/**
 * Removes config the server refuses for the node's current shape: a `run_artifact` sink takes no
 * `connection_id`, `path_template` or `method` (schema defaults for them must not leak into the config).
 */
export function pruneConfig(
  node: PipelineNode,
  config: Record<string, unknown>,
): Record<string, unknown> {
  if (node.type !== 'sink' || config.target === 'connection') return config;
  const next = { ...config };
  for (const k of SINK_CONNECTION_KEYS) delete next[k];
  return next;
}

export interface ConfigLayout {
  resources: ResourceKey[];
  /** Fields the schema form can render. */
  fields: FieldDef[];
  /** Properties of the schema that need the JSON editor (objects, arrays of objects, ...). */
  jsonKeys: string[];
  /** The transform operator editor replaces `operations`. */
  hasOperators: boolean;
}

/** Splits a node type's config schema into pickers, schema-form fields and JSON-only properties. */
export function configLayout(node: PipelineNode, schema: JsonSchema | undefined): ConfigLayout {
  const resources = resourceKeysFor(node, schema);
  const hasOperators = node.type === 'transform';
  const skip = [
    ...RESOURCE_KEYS,
    ...(hasOperators ? ['operations'] : []),
    ...(node.type === 'sink' && !isConnectionSink(node) ? ['method', 'path_template'] : []),
  ];
  const fields = fieldsFromSchema(schema, skip);
  const rendered = new Set(fields.map((f) => f.key));
  const jsonKeys = Object.keys(schema?.properties ?? {}).filter(
    (k) => !skip.includes(k) && !rendered.has(k),
  );
  return { resources, fields, jsonKeys, hasOperators };
}

/** Inverse of `buildConfig`: form text for each field from the stored config (schema defaults when unset). */
export function valuesFromConfig(fields: FieldDef[], config: Record<string, unknown>): FormValues {
  const v = initialValues(fields);
  for (const f of fields) {
    const raw = config[f.key];
    if (raw === undefined || raw === null) continue;
    if (f.kind === 'boolean') v[f.key] = raw === true;
    else if (f.kind === 'stringArray') v[f.key] = Array.isArray(raw) ? raw.join('\n') : '';
    else v[f.key] = String(raw);
  }
  return v;
}

/** Replaces the form-managed keys of `config` with `built`, leaving every other key untouched. */
export function mergeConfig(
  config: Record<string, unknown>,
  fields: FieldDef[],
  built: Record<string, unknown>,
): Record<string, unknown> {
  const next = { ...config };
  for (const f of fields) delete next[f.key];
  return { ...next, ...built };
}
