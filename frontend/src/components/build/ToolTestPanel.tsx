'use client';

import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { ToolSpec, ToolTestResult } from '@/lib/api/build-types';
import type { JsonSchema } from '@/lib/api/integrate-types';
import {
  buildConfig,
  fieldsFromSchema,
  initialValues,
  validateValues,
  type FormValues,
} from '@/lib/schema-form';
import { isWriteClass, SIDE_EFFECT_WARNINGS } from '@/lib/tool-rules';
import { ActionError, Badge, useAction } from '../admin/ui';
import { SchemaForm } from '../integrate/SchemaForm';
import type { TestPanelProps } from './DraftEditor';
import { Area, JsonView } from './shared';

export function ToolTestPanel({ org, artifact, spec, dirty }: TestPanelProps<ToolSpec>) {
  const schema = spec.input_schema as JsonSchema;
  const fields = fieldsFromSchema(schema);
  const rendered = new Set(fields.map((f) => f.key));
  const skipped = Object.keys(schema.properties ?? {}).filter((k) => !rendered.has(k));
  const [values, setValues] = useState<FormValues>(() => initialValues(fields));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [extra, setExtra] = useState('');
  const [extraError, setExtraError] = useState<string | null>(null);
  const [ack, setAck] = useState(false);
  const [result, setResult] = useState<ToolTestResult | null>(null);
  const { busy, error, run } = useAction();
  const sideEffects = isWriteClass(spec.side_effect_class);
  const merged: FormValues = { ...initialValues(fields), ...values };

  async function test() {
    const errs = validateValues(fields, merged);
    setErrors(errs);
    let args: Record<string, unknown> = buildConfig(fields, merged);
    setExtraError(null);
    if (extra.trim()) {
      try {
        const parsed: unknown = JSON.parse(extra);
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed))
          throw new Error('must be a JSON object');
        args = { ...args, ...(parsed as Record<string, unknown>) };
      } catch (e) {
        setExtraError(
          `Additional arguments ${e instanceof Error ? e.message : 'are not valid JSON'}.`,
        );
        return;
      }
    }
    if (Object.keys(errs).length > 0) return;
    const r = await run(() =>
      endpoints.testTool(org, artifact.id, 'draft', args, sideEffects && ack),
    );
    if (r.ok) setResult(r.value);
  }

  return (
    <section aria-label="Test panel" className="card space-y-3">
      <h3 className="text-sm font-semibold">Test tool</h3>
      <p className="text-xs text-slate-600">Runs the saved draft with the arguments below.</p>
      <SchemaForm
        fields={fields}
        values={merged}
        errors={errors}
        onChange={(k, v) => setValues((cur) => ({ ...cur, [k]: v }))}
        emptyText="The input schema declares no simple arguments."
      />
      {(skipped.length > 0 || fields.length === 0) && (
        <>
          {skipped.length > 0 && (
            <p className="text-xs text-slate-600">
              Structured arguments are entered as JSON: {skipped.join(', ')}.
            </p>
          )}
          <Area
            label="Additional arguments (JSON object)"
            value={extra}
            rows={3}
            monospace
            onChange={setExtra}
          />
          {extraError && (
            <p role="alert" className="text-sm text-red-700">
              {extraError}
            </p>
          )}
        </>
      )}
      {sideEffects && (
        <div role="note" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm">
          <p className="font-medium text-amber-900">
            This tool has side effects ({spec.side_effect_class}).
          </p>
          <p className="mt-1 text-amber-900">{SIDE_EFFECT_WARNINGS[spec.side_effect_class]}</p>
          <p className="mt-1 text-amber-900">Requires the tool:test_side_effects permission.</p>
          <label className="mt-2 flex items-center gap-2 text-sm font-medium text-amber-900">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />I
            understand this test run performs real side effects
          </label>
        </div>
      )}
      {dirty && <p className="text-xs text-amber-900">Save the draft to test your latest edits.</p>}
      <button
        type="button"
        className="btn btn-primary"
        disabled={busy || dirty || (sideEffects && !ack)}
        onClick={() => void test()}
      >
        {busy ? 'Running...' : 'Run test'}
      </button>
      <ActionError error={error} title="Tool test failed" />
      {result && (
        <div className="space-y-2" aria-label="Tool test result">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Badge tone={result.valid_output ? 'good' : 'bad'}>
              {result.valid_output ? 'Output matches schema' : 'Output does not match schema'}
            </Badge>
            <span>Duration: {result.duration_ms} ms</span>
            <span>Attempts: {result.attempts}</span>
          </div>
          <JsonView value={result.output} label="Tool output" />
        </div>
      )}
    </section>
  );
}
