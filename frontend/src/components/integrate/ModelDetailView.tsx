'use client';

import Link from 'next/link';
import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type {
  ChatMessage,
  InvokeModelResult,
  Model,
  ModelLifecycle,
} from '@/lib/api/integrate-types';
import { useCan } from '@/lib/hooks';
import { useAdapters, useModel, useProviders, useRefreshInt } from '@/lib/integrate-hooks';
import { isPlannedAdapter, PLANNED_EXPLANATION } from '@/lib/maturity';
import { ConfirmDialog } from '../ConfirmDialog';
import { ActionError, PageHeader, QueryGate, SelectField, TextField, useAction } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { StatusBadge } from './Maturity';

const NEXT: Record<ModelLifecycle, ModelLifecycle[]> = {
  preview: ['active', 'deprecated', 'retired'],
  active: ['deprecated', 'retired'],
  deprecated: ['active', 'retired'],
  retired: [], // terminal
};

export function ModelDetailView({ id }: { id: string }) {
  const org = useOrgParam();
  const { can } = useCan(org);
  const model = useModel(org, id);
  const providers = useProviders(org);
  const adapters = useAdapters();

  return (
    <div className="space-y-6">
      <p className="text-sm">
        <Link
          className="text-blue-800 underline"
          href={`/o/${encodeURIComponent(org)}/integrate/models`}
        >
          Back to models
        </Link>
      </p>
      <QueryGate query={model} noun="model">
        {(m) => {
          const provider = providers.data?.items.find((p) => p.id === m.provider_id);
          const adapter = adapters.data?.find((a) => a.key === provider?.adapter_key);
          const planned = adapters.isSuccess && provider !== undefined && isPlannedAdapter(adapter);
          return (
            <>
              <PageHeader title={m.display_name} description={m.model_key} />
              <dl className="card grid gap-3 text-sm sm:grid-cols-2">
                <Item label="Provider">{provider?.name ?? m.provider_id}</Item>
                <Item label="Lifecycle">
                  <StatusBadge status={m.lifecycle} />
                </Item>
                <Item label="Capabilities">{m.capabilities.join(', ')}</Item>
                <Item label="Context window">{m.context_window.toLocaleString()} tokens</Item>
                <Item label="Max output">{m.max_output_tokens.toLocaleString()} tokens</Item>
                <Item label="Rate limits">
                  {m.rate_limits && Object.keys(m.rate_limits).length > 0
                    ? Object.entries(m.rate_limits)
                        .map(([k, v]) => `${k}: ${v}`)
                        .join(', ')
                    : 'None set'}
                </Item>
                {m.deprecation_date && <Item label="Deprecation date">{m.deprecation_date}</Item>}
              </dl>
              {can('model:manage') && <Lifecycle org={org} model={m} />}
              {can('model:invoke') && <InvokeConsole org={org} model={m} planned={planned} />}
            </>
          );
        }}
      </QueryGate>
    </div>
  );
}

function Item({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="font-medium text-slate-600">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Lifecycle({ org, model }: { org: string; model: Model }) {
  const refresh = useRefreshInt(org);
  const action = useAction();
  const [target, setTarget] = useState<ModelLifecycle | null>(null);
  const options = NEXT[model.lifecycle];

  async function apply(to: ModelLifecycle) {
    const res = await action.run(() =>
      endpoints.updateModel(org, model.id, { lifecycle: to, row_version: model.row_version }),
    );
    setTarget(null);
    if (res.ok) await refresh('models');
  }

  return (
    <section aria-labelledby="life-h" className="card space-y-3">
      <h2 id="life-h" className="text-lg font-semibold">
        Lifecycle
      </h2>
      {options.length === 0 ? (
        <p className="text-sm text-slate-600">This model is retired. Retirement is terminal.</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {options.map((to) => (
            <button
              key={to}
              type="button"
              className="btn"
              onClick={() => (to === 'retired' ? setTarget(to) : void apply(to))}
              disabled={action.busy}
            >
              {to === 'active' ? 'Mark active' : to === 'deprecated' ? 'Deprecate' : 'Retire'}
            </button>
          ))}
        </div>
      )}
      <ActionError error={action.error} title="Could not change the lifecycle" />
      <ConfirmDialog
        open={target !== null}
        title="Retire model?"
        confirmLabel="Retire model"
        busy={action.busy}
        onConfirm={() => target && void apply(target)}
        onCancel={() => setTarget(null)}
      >
        <p>
          <strong>{model.display_name}</strong> will stop being usable. Retirement cannot be undone.
        </p>
      </ConfirmDialog>
    </section>
  );
}

/** Governed test call. The server enforces allowlist, lifecycle, provider status and rate limits. */
function InvokeConsole({ org, model, planned }: { org: string; model: Model; planned: boolean }) {
  const action = useAction();
  const [rows, setRows] = useState<ChatMessage[]>([{ role: 'user', content: '' }]);
  const [maxTokens, setMaxTokens] = useState('');
  const [result, setResult] = useState<InvokeModelResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const disabled = planned || model.lifecycle === 'retired';

  async function run() {
    const messages = rows.filter((r) => r.content.trim() !== '');
    if (messages.length === 0) {
      setError('Enter at least one message.');
      return;
    }
    const max = maxTokens.trim() === '' ? undefined : Number(maxTokens);
    if (max !== undefined && (!Number.isInteger(max) || max < 1)) {
      setError('Max tokens must be a positive whole number.');
      return;
    }
    setError(null);
    const res = await action.run(() => endpoints.invokeModel(org, model.id, messages, max));
    if (res.ok) setResult(res.value);
  }

  return (
    <section aria-labelledby="invoke-h" className="card space-y-4">
      <h2 id="invoke-h" className="text-lg font-semibold">
        Test invoke
      </h2>
      {planned ? (
        <p role="note" className="rounded-md border border-slate-300 bg-slate-100 p-3 text-sm">
          {PLANNED_EXPLANATION} Invocation is disabled.
        </p>
      ) : (
        <p className="text-sm text-slate-600">
          A governed call: allowlists, lifecycle, provider status and rate limits are enforced and
          the call is audited. Prompt content is not stored in the audit log.
        </p>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
        className="space-y-3"
        noValidate
      >
        {rows.map((r, i) => (
          <div key={i} className="grid gap-2 sm:grid-cols-[10rem_1fr]">
            <SelectField
              label={`Role of message ${i + 1}`}
              value={r.role}
              disabled={disabled}
              onChange={(v) =>
                setRows((cur) =>
                  cur.map((x, j) => (j === i ? { ...x, role: v as ChatMessage['role'] } : x)),
                )
              }
              options={['system', 'user', 'assistant'].map((v) => ({ value: v, label: v }))}
            />
            <TextField
              label={`Message ${i + 1}`}
              value={r.content}
              multiline
              disabled={disabled}
              onChange={(v) =>
                setRows((cur) => cur.map((x, j) => (j === i ? { ...x, content: v } : x)))
              }
            />
          </div>
        ))}
        <div className="flex flex-wrap items-end gap-3">
          <button
            type="button"
            className="btn"
            disabled={disabled}
            onClick={() => setRows((cur) => [...cur, { role: 'user', content: '' }])}
          >
            Add message
          </button>
          <TextField
            label="Max tokens"
            type="number"
            value={maxTokens}
            onChange={setMaxTokens}
            disabled={disabled}
          />
          <button type="submit" className="btn btn-primary" disabled={disabled || action.busy}>
            {action.busy ? 'Invoking...' : 'Invoke model'}
          </button>
        </div>
      </form>
      {error && (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
      <ActionError error={action.error} title="Invocation failed" />
      {result && (
        <div aria-label="Invocation result" role="region" className="space-y-2">
          <pre className="max-h-80 overflow-auto rounded-md border border-slate-300 bg-slate-100 p-3 text-sm whitespace-pre-wrap">
            {result.output}
          </pre>
          <dl className="grid gap-2 text-sm sm:grid-cols-4">
            <Item label="Input tokens">{result.usage.input_tokens}</Item>
            <Item label="Output tokens">{result.usage.output_tokens}</Item>
            <Item label="Latency">{result.latency_ms} ms</Item>
            {result.provider_request_id && (
              <Item label="Provider request ID">
                <span className="font-mono">{result.provider_request_id}</span>
              </Item>
            )}
          </dl>
        </div>
      )}
    </section>
  );
}
