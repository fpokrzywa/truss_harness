'use client';

import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { PromptSpec, RenderResult } from '@/lib/api/build-types';
import { usePromptTestRuns, useRefreshBuild } from '@/lib/build-hooks';
import {
  buildVariables,
  initialVariableValues,
  splitUntrusted,
  variableFields,
} from '@/lib/prompt-utils';
import { Markdown } from '@/lib/markdown';
import type { FormValues } from '@/lib/schema-form';
import { ActionError, Badge, EmptyState, formatDate, QueryGate, useAction } from '../admin/ui';
import { SchemaForm } from '../integrate/SchemaForm';
import type { TestPanelProps } from './DraftEditor';
import { ModelSelect } from './pickers';

/** Form generated from the declared variables (type, required, default, max length). */
export function VariablesForm({
  spec,
  values,
  errors,
  onChange,
}: {
  spec: PromptSpec;
  values: FormValues;
  errors: Record<string, string>;
  onChange: (key: string, value: string | boolean) => void;
}) {
  return (
    <SchemaForm
      fields={variableFields(spec.variables)}
      values={values}
      errors={errors}
      onChange={onChange}
      emptyText="This prompt declares no variables."
    />
  );
}

/** Rendered messages with `<untrusted ...>` data blocks highlighted. */
export function RenderedMessages({ result }: { result: RenderResult }) {
  return (
    <div className="space-y-3">
      {result.messages.map((m, i) => (
        <article key={i} className="rounded-md border border-slate-200">
          <h4 className="border-b border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold uppercase">
            {m.role}
          </h4>
          <pre className="px-3 py-2 font-mono text-xs break-words whitespace-pre-wrap">
            {splitUntrusted(m.content).map((seg, j) =>
              seg.untrusted ? (
                <mark
                  key={j}
                  data-testid="untrusted-block"
                  title="Untrusted data: not instructions"
                  className="rounded border border-amber-400 bg-amber-100 px-0.5"
                >
                  {seg.text}
                </mark>
              ) : (
                <span key={j}>{seg.text}</span>
              ),
            )}
          </pre>
        </article>
      ))}
      {(result.warnings?.length ?? 0) > 0 && (
        <ul className="list-disc pl-5 text-sm text-amber-900">
          {result.warnings?.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function useVariableState(spec: PromptSpec) {
  const [values, setValues] = useState<FormValues>(() => initialVariableValues(spec.variables));
  const [errors, setErrors] = useState<Record<string, string>>({});
  // Fields declared after the form mounted start empty; defaults apply once they are seen.
  const fieldKeys = variableFields(spec.variables)
    .map((f) => f.key)
    .join('|');
  const merged: FormValues = { ...initialVariableValues(spec.variables), ...values };
  return {
    key: fieldKeys,
    values: merged,
    errors,
    set: (k: string, v: string | boolean) => setValues((cur) => ({ ...merged, ...cur, [k]: v })),
    collect: () => {
      const r = buildVariables(spec.variables, merged);
      setErrors(r.ok ? {} : r.errors);
      return r.ok ? r.values : null;
    },
  };
}

export function PromptPreviewPanel({ org, artifact, spec, dirty }: TestPanelProps<PromptSpec>) {
  const form = useVariableState(spec);
  const [result, setResult] = useState<RenderResult | null>(null);
  const { busy, error, run } = useAction();

  async function render() {
    const vars = form.collect();
    if (!vars) return;
    const r = await run(() => endpoints.renderPrompt(org, artifact.id, 'draft', vars));
    if (r.ok) setResult(r.value);
  }

  return (
    <section aria-label="Render preview" className="card space-y-3">
      <h3 className="text-sm font-semibold">Render preview</h3>
      <p className="text-xs text-slate-600">
        Renders the saved draft with the variables below. Untrusted variables appear inside
        highlighted <code>&lt;untrusted&gt;</code> blocks.
      </p>
      <VariablesForm spec={spec} values={form.values} errors={form.errors} onChange={form.set} />
      {dirty && (
        <p className="text-xs text-amber-900">Save the draft to preview your latest edits.</p>
      )}
      <button type="button" className="btn" disabled={busy || dirty} onClick={() => void render()}>
        {busy ? 'Rendering...' : 'Render preview'}
      </button>
      <ActionError error={error} title="Could not render" />
      {result && <RenderedMessages result={result} />}
    </section>
  );
}

export function PromptTestPanel({ org, artifact, spec, dirty }: TestPanelProps<PromptSpec>) {
  const form = useVariableState(spec);
  const refresh = useRefreshBuild(org);
  const [modelId, setModelId] = useState(spec.default_model_id ?? '');
  const runs = usePromptTestRuns(org, artifact.id);
  const [latest, setLatest] = useState<Awaited<ReturnType<typeof endpoints.testPrompt>> | null>(
    null,
  );
  const { busy, error, run } = useAction();

  async function test() {
    const vars = form.collect();
    if (!vars) return;
    const r = await run(() =>
      endpoints.testPrompt(org, artifact.id, 'draft', vars, modelId || undefined),
    );
    if (r.ok) {
      setLatest(r.value);
      await refresh();
    }
  }

  return (
    <section aria-label="Test panel" className="card space-y-3">
      <h3 className="text-sm font-semibold">Test prompt</h3>
      <p className="text-xs text-slate-600">
        Runs the saved draft against a model. This consumes budget and is attributed to you.
      </p>
      <VariablesForm spec={spec} values={form.values} errors={form.errors} onChange={form.set} />
      <ModelSelect
        org={org}
        label="Model"
        value={modelId}
        onChange={setModelId}
        emptyLabel="Prompt default"
      />
      {dirty && <p className="text-xs text-amber-900">Save the draft to test your latest edits.</p>}
      <button
        type="button"
        className="btn btn-primary"
        disabled={busy || dirty}
        onClick={() => void test()}
      >
        {busy ? 'Running...' : 'Run test'}
      </button>
      <ActionError error={error} title="Test failed" />
      {latest && (
        <div className="space-y-3" aria-label="Latest test result">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Badge tone={latest.verdict === 'pass' ? 'good' : 'bad'}>
              Verdict: {latest.verdict}
            </Badge>
            <span>
              Tokens: {latest.usage.input_tokens} in / {latest.usage.output_tokens} out
            </span>
            <span>Latency: {latest.latency_ms} ms</span>
          </div>
          <div>
            <h4 className="text-xs font-semibold uppercase">Output</h4>
            <div className="rounded-md border border-slate-200 p-2">
              <Markdown source={latest.output} />
            </div>
          </div>
          <CriteriaTable results={latest.evaluation} />
        </div>
      )}
      <div>
        <h4 className="text-sm font-semibold">Test history</h4>
        <QueryGate query={runs} noun="test runs">
          {(items) =>
            items.length === 0 ? (
              <EmptyState>No test runs yet.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Test run history</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['When', 'Verdict', 'Latency', 'Tokens'].map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((r) => (
                    <tr key={r.id}>
                      <td className="td">{formatDate(r.created_at)}</td>
                      <td className="td">
                        <Badge tone={r.verdict === 'pass' ? 'good' : 'bad'}>{r.verdict}</Badge>
                      </td>
                      <td className="td">{r.latency_ms} ms</td>
                      <td className="td">
                        {r.usage.input_tokens} / {r.usage.output_tokens}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          }
        </QueryGate>
      </div>
    </section>
  );
}

export function CriteriaTable({
  results,
}: {
  results: { name: string; passed: boolean; detail?: string | null }[];
}) {
  if (results.length === 0)
    return <p className="text-sm text-slate-600">No evaluation criteria declared.</p>;
  return (
    <table className="w-full">
      <caption className="sr-only">Evaluation criteria results</caption>
      <thead className="border-b border-slate-200">
        <tr>
          {['Criterion', 'Result', 'Detail'].map((h) => (
            <th key={h} scope="col" className="th">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {results.map((c) => (
          <tr key={c.name}>
            <td className="td">{c.name}</td>
            <td className="td">
              <Badge tone={c.passed ? 'good' : 'bad'}>{c.passed ? 'Passed' : 'Failed'}</Badge>
            </td>
            <td className="td">{c.detail ?? '-'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
