'use client';

import { useQuery } from '@tanstack/react-query';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import type {
  ExecutorDescriptor,
  SideEffectClass,
  ToolExecutor,
  ToolSpec,
} from '@/lib/api/build-types';
import type { Discovery } from '@/lib/api/integrate-types';
import { useToolExecutors } from '@/lib/build-hooks';
import {
  retryErrors,
  schemaErrors,
  SIDE_EFFECT_CLASSES,
  SIDE_EFFECT_WARNINGS,
} from '@/lib/tool-rules';
import { PLANNED_LABEL } from '../integrate/Maturity';
import { SelectField, TextField } from '../admin/ui';
import type { EditorProps } from './DraftEditor';
import { ConnectionSelect } from './pickers';
import { Area, JsonField, JsonView, num, NumberField, Section } from './shared';

const FALLBACK_EXECUTORS: ExecutorDescriptor[] = [
  { key: 'builtin.function' },
  { key: 'rest.http' },
  { key: 'mcp.tool' },
  { key: 'connection.sql' },
];

export function defaultToolSpec(): ToolSpec {
  return {
    executor: 'builtin.function',
    binding: {},
    input_schema: { type: 'object', properties: {} },
    output_schema: { type: 'object', properties: {} },
    side_effect_class: 'none',
    required_permissions: [],
    invocation_policy: {},
    timeout_s: 30,
    retry: { max_attempts: 1, backoff_s: 1 },
    audit_level: 'metadata',
    idempotency: 'none',
  };
}

export function normalizeToolSpec(raw: Record<string, unknown> | undefined): ToolSpec {
  const d = defaultToolSpec();
  if (!raw) return d;
  const r = raw as Partial<ToolSpec>;
  return {
    ...d,
    ...r,
    binding: r.binding ?? {},
    invocation_policy: r.invocation_policy ?? {},
    retry: { ...d.retry, ...(r.retry ?? {}) },
    required_permissions: r.required_permissions ?? [],
  };
}

export const toolBlockers = (spec: ToolSpec): string[] => retryErrors(spec);

const lines = (s: string) =>
  s
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);

/** Tools array of a stored discovery, tolerating the payload under `payload`, `result`, `data` or top level. */
export function rawMcpTools(d: Discovery | null | undefined): Record<string, unknown>[] {
  if (!d) return [];
  const rec = d as Record<string, unknown>;
  const isRec = (v: unknown): v is Record<string, unknown> =>
    typeof v === 'object' && v !== null && !Array.isArray(v);
  const base = [rec.payload, rec.result, rec.data].find(isRec) ?? rec;
  const tools = (base as Record<string, unknown>).tools;
  return Array.isArray(tools) ? tools.filter(isRec) : [];
}

function useLatestDiscovery(org: string, connectionId: string) {
  return useQuery({
    queryKey: ['int', org, 'connections', 'discovery-latest', connectionId],
    enabled: !!connectionId,
    queryFn: async () => {
      try {
        return await endpoints.latestDiscovery(org, connectionId);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null; // none discovered yet
        throw e;
      }
    },
  });
}

export function ToolEditor({
  org,
  spec,
  onChange,
  readOnly,
  reportInvalid,
}: EditorProps<ToolSpec>) {
  const catalog = useToolExecutors();
  const executors = catalog.data?.items.length ? catalog.data.items : FALLBACK_EXECUTORS;
  const set = (patch: Partial<ToolSpec>) => onChange({ ...spec, ...patch });
  const setBinding = (patch: Record<string, unknown>) =>
    set({ binding: { ...spec.binding, ...patch } });
  const descriptor = executors.find((e) => e.key === spec.executor);
  const errors = retryErrors(spec);

  return (
    <div className="space-y-4">
      <Section title="Executor and binding">
        <SelectField
          label="Executor"
          value={spec.executor}
          disabled={readOnly}
          onChange={(k) => set({ executor: k as ToolExecutor, binding: {} })}
          options={executors.map((e) => ({
            value: e.key,
            label: `${e.label ?? e.key}${e.maturity === 'planned' ? ` (${PLANNED_LABEL})` : ''}`,
          }))}
        />
        {descriptor?.maturity === 'planned' && (
          <p role="note" className="text-sm text-amber-900">
            This executor is declared but not validated; publishing a tool that uses it is rejected.
          </p>
        )}
        {descriptor?.description && (
          <p className="text-xs text-slate-600">{descriptor.description}</p>
        )}
        <BindingForm
          org={org}
          spec={spec}
          descriptor={descriptor}
          readOnly={readOnly}
          setBinding={setBinding}
          set={set}
          reportInvalid={reportInvalid}
        />
      </Section>

      <Section
        title="Schemas"
        help="JSON Schema draft 2020-12. Arguments are validated against the input schema; results against the output schema."
      >
        <JsonField
          label="Input schema"
          value={spec.input_schema}
          readOnly={readOnly}
          rows={8}
          check={schemaErrors}
          onChange={(v) => set({ input_schema: (v ?? {}) as Record<string, unknown> })}
          onInvalid={(m) => reportInvalid('input_schema', m)}
        />
        <JsonField
          label="Output schema"
          value={spec.output_schema}
          readOnly={readOnly}
          rows={8}
          check={schemaErrors}
          onChange={(v) => set({ output_schema: (v ?? {}) as Record<string, unknown> })}
          onInvalid={(m) => reportInvalid('output_schema', m)}
        />
      </Section>

      <Section title="Side effects">
        <SelectField
          label="Side-effect class"
          value={spec.side_effect_class}
          disabled={readOnly}
          onChange={(c) => {
            const cls = c as SideEffectClass;
            set({
              side_effect_class: cls,
              // write_idempotent needs an idempotency key; offer it rather than leave an invalid combination.
              ...(cls === 'write_idempotent' ? { idempotency: 'key_supported' as const } : {}),
            });
          }}
          options={SIDE_EFFECT_CLASSES.map((c) => ({ value: c, label: c }))}
        />
        <p
          role="note"
          className={`rounded-md border p-2 text-sm ${
            spec.side_effect_class === 'none' || spec.side_effect_class === 'read'
              ? 'border-slate-200 bg-slate-50 text-slate-700'
              : 'border-amber-300 bg-amber-50 text-amber-900'
          }`}
        >
          {SIDE_EFFECT_WARNINGS[spec.side_effect_class]}
        </p>
        <SelectField
          label="Idempotency"
          value={spec.idempotency}
          disabled={readOnly}
          onChange={(v) => set({ idempotency: v as ToolSpec['idempotency'] })}
          options={[
            { value: 'none', label: 'none' },
            { value: 'key_supported', label: 'key_supported' },
          ]}
        />
      </Section>

      <Section title="Policy, limits and audit">
        <Area
          label="Required permissions (one per line)"
          rows={3}
          readOnly={readOnly}
          value={spec.required_permissions.join('\n')}
          onChange={(v) => set({ required_permissions: lines(v) })}
        />
        <div className="grid gap-3 sm:grid-cols-2">
          <TextField
            label="Max calls per run"
            type="number"
            disabled={readOnly}
            value={spec.invocation_policy.max_calls_per_run?.toString() ?? ''}
            onChange={(v) =>
              set({ invocation_policy: { ...spec.invocation_policy, max_calls_per_run: num(v) } })
            }
          />
          <Area
            label="Allowed agent ids (one per line, empty = any)"
            rows={2}
            readOnly={readOnly}
            value={(spec.invocation_policy.allowed_agent_ids ?? []).join('\n')}
            onChange={(v) =>
              set({
                invocation_policy: {
                  ...spec.invocation_policy,
                  allowed_agent_ids: lines(v).length ? lines(v) : undefined,
                },
              })
            }
          />
          <NumberField
            label="Timeout (seconds)"
            min={1}
            fallback={30}
            disabled={readOnly}
            value={spec.timeout_s}
            onChange={(v) => set({ timeout_s: v })}
          />
          <NumberField
            label="Retry max attempts"
            min={1}
            fallback={1}
            disabled={readOnly}
            value={spec.retry.max_attempts}
            onChange={(v) => set({ retry: { ...spec.retry, max_attempts: v } })}
            error={errors.find((e) => e.startsWith('Retries')) ?? null}
          />
          <NumberField
            label="Retry backoff (seconds)"
            min={0}
            fallback={0}
            disabled={readOnly}
            value={spec.retry.backoff_s}
            onChange={(v) => set({ retry: { ...spec.retry, backoff_s: v } })}
          />
          <SelectField
            label="Audit level"
            value={spec.audit_level}
            disabled={readOnly}
            onChange={(v) => set({ audit_level: v as ToolSpec['audit_level'] })}
            options={[
              { value: 'metadata', label: 'metadata (no payloads)' },
              { value: 'full', label: 'full (includes payloads)' },
            ]}
          />
        </div>
      </Section>
    </div>
  );
}

function BindingForm({
  org,
  spec,
  descriptor,
  readOnly,
  setBinding,
  set,
  reportInvalid,
}: {
  org: string;
  spec: ToolSpec;
  descriptor: ExecutorDescriptor | undefined;
  readOnly: boolean;
  setBinding: (patch: Record<string, unknown>) => void;
  set: (patch: Partial<ToolSpec>) => void;
  reportInvalid: EditorProps<ToolSpec>['reportInvalid'];
}) {
  const b = spec.binding;
  const str = (k: string) => (typeof b[k] === 'string' ? (b[k] as string) : '');
  const connectionId = str('connection_id');
  const discovery = useLatestDiscovery(org, spec.executor === 'mcp.tool' ? connectionId : '');

  if (spec.executor === 'builtin.function') {
    const fns = descriptor?.functions ?? [];
    const chosen = fns.find((f) => f.key === str('function_key'));
    return (
      <div className="space-y-3">
        <SelectField
          label="Builtin function"
          value={str('function_key')}
          disabled={readOnly}
          onChange={(k) => setBinding({ function_key: k })}
          options={[
            { value: '', label: 'Select a function...' },
            ...fns.map((f) => ({ value: f.key, label: f.key })),
          ]}
        />
        {chosen && (
          <div className="space-y-2">
            {chosen.description && <p className="text-sm text-slate-700">{chosen.description}</p>}
            {chosen.input_schema && (
              <JsonView value={chosen.input_schema} label="Function input schema" />
            )}
            {chosen.output_schema && (
              <JsonView value={chosen.output_schema} label="Function output schema" />
            )}
            {!readOnly && (
              <button
                type="button"
                className="btn"
                onClick={() =>
                  set({
                    ...(chosen.input_schema ? { input_schema: chosen.input_schema } : {}),
                    ...(chosen.output_schema ? { output_schema: chosen.output_schema } : {}),
                    ...(chosen.side_effect_class
                      ? { side_effect_class: chosen.side_effect_class }
                      : {}),
                  })
                }
              >
                Copy schemas from function
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  if (spec.executor === 'rest.http') {
    return (
      <div className="space-y-3">
        <ConnectionSelect
          org={org}
          label="REST connection"
          types={['rest']}
          value={connectionId}
          disabled={readOnly}
          onChange={(id) => setBinding({ connection_id: id })}
        />
        <div className="grid gap-3 sm:grid-cols-4">
          <SelectField
            label="Method"
            value={str('method') || 'GET'}
            disabled={readOnly}
            onChange={(m) => setBinding({ method: m })}
            options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => ({ value: m, label: m }))}
          />
          <div className="sm:col-span-3">
            <TextField
              label="Path template"
              value={str('path_template')}
              disabled={readOnly}
              monospace
              help="Use {{arg}} placeholders filled from the tool arguments."
              onChange={(v) => setBinding({ path_template: v })}
            />
          </div>
        </div>
        <JsonField
          label="Query template (JSON object, optional)"
          value={b.query_template}
          readOnly={readOnly}
          rows={3}
          onChange={(v) => setBinding({ query_template: v })}
          onInvalid={(m) => reportInvalid('query_template', m)}
        />
        <JsonField
          label="Body template (JSON, optional)"
          value={b.body_template}
          readOnly={readOnly}
          rows={4}
          onChange={(v) => setBinding({ body_template: v })}
          onInvalid={(m) => reportInvalid('body_template', m)}
        />
      </div>
    );
  }

  if (spec.executor === 'mcp.tool') {
    const tools = rawMcpTools(discovery.data);
    const names = tools.map((t) => String(t.name));
    const current = str('tool_name');
    return (
      <div className="space-y-3">
        <ConnectionSelect
          org={org}
          label="MCP connection"
          types={['mcp']}
          value={connectionId}
          disabled={readOnly}
          onChange={(id) =>
            setBinding({ connection_id: id, tool_name: '', schema_snapshot: undefined })
          }
        />
        {connectionId && discovery.isSuccess && discovery.data === null && (
          <p className="text-sm text-amber-900">
            This connection has no discovery results yet. Run discovery from the connection page.
          </p>
        )}
        <SelectField
          label="MCP tool"
          value={current}
          disabled={readOnly || !connectionId}
          onChange={(name) => {
            const t = tools.find((x) => x.name === name);
            const input = t?.input_schema;
            // One update: two set() calls in a handler would each start from the same stale spec.
            set({
              binding: {
                ...spec.binding,
                tool_name: name,
                schema_snapshot: t
                  ? { name: t.name, description: t.description, input_schema: t.input_schema }
                  : undefined,
              },
              ...(input && typeof input === 'object'
                ? { input_schema: input as Record<string, unknown> }
                : {}),
            });
          }}
          options={[
            { value: '', label: 'Select a tool...' },
            ...(current && !names.includes(current) ? [{ value: current, label: current }] : []),
            ...names.map((n) => ({ value: n, label: n })),
          ]}
        />
        <p className="text-xs text-slate-600">
          The tool&apos;s schema is snapshotted into the binding and pinned at publish; later
          changes on the MCP server are not exposed.
        </p>
        {b.schema_snapshot !== undefined && (
          <JsonView value={b.schema_snapshot} label="Schema snapshot" />
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <ConnectionSelect
        org={org}
        label="Database connection"
        types={['database']}
        value={connectionId}
        disabled={readOnly}
        onChange={(id) => setBinding({ connection_id: id })}
      />
      <Area
        label="SQL template (parameterized)"
        value={str('sql_template')}
        readOnly={readOnly}
        rows={5}
        monospace
        help="Use named parameters such as :customer_id bound from tool arguments. Never concatenate arguments into SQL."
        onChange={(v) => setBinding({ sql_template: v })}
      />
      <TextField
        label="Max rows"
        type="number"
        min={1}
        disabled={readOnly}
        value={typeof b.max_rows === 'number' ? String(b.max_rows) : ''}
        onChange={(v) => setBinding({ max_rows: num(v) })}
      />
    </div>
  );
}
