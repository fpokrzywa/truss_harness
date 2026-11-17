'use client';

import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { AgentSpec, AgentTestResult } from '@/lib/api/build-types';
import { Markdown } from '@/lib/markdown';
import { ActionError, useAction } from '../admin/ui';
import type { TestPanelProps } from './DraftEditor';
import { RenderedMessages } from './PromptPanels';
import { Area, JsonView } from './shared';

export function AgentTestPanel({ org, artifact, dirty }: TestPanelProps<AgentSpec>) {
  const [variables, setVariables] = useState('{}');
  const [input, setInput] = useState('');
  const [varError, setVarError] = useState<string | null>(null);
  const [result, setResult] = useState<AgentTestResult | null>(null);
  const { busy, error, run } = useAction();

  async function test() {
    let vars: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(variables || '{}');
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed))
        throw new Error('must be a JSON object');
      vars = parsed as Record<string, unknown>;
    } catch (e) {
      setVarError(`Variables ${e instanceof Error ? e.message : 'are not valid JSON'}.`);
      return;
    }
    setVarError(null);
    const r = await run(() => endpoints.testAgent(org, artifact.id, 'draft', vars, input));
    if (r.ok) setResult(r.value);
  }

  return (
    <section aria-label="Assembly test" className="card space-y-3">
      <h3 className="text-sm font-semibold">Assembly test</h3>
      <p role="note" className="text-xs text-slate-600">
        Assembles the saved draft (system, skills, prompt and tool declarations) and performs one
        model call. Tool execution arrives with the execution harness; no tools are run here.
      </p>
      <Area
        label="Variables (JSON object)"
        value={variables}
        rows={3}
        monospace
        onChange={setVariables}
      />
      {varError && (
        <p role="alert" className="text-sm text-red-700">
          {varError}
        </p>
      )}
      <Area label="Input" value={input} rows={3} onChange={setInput} />
      {dirty && <p className="text-xs text-amber-900">Save the draft to test your latest edits.</p>}
      <button
        type="button"
        className="btn btn-primary"
        disabled={busy || dirty}
        onClick={() => void test()}
      >
        {busy ? 'Running...' : 'Run assembly test'}
      </button>
      <ActionError error={error} title="Assembly test failed" />
      {result && (
        <div className="space-y-3" aria-label="Assembly test result">
          <div>
            <h4 className="text-xs font-semibold uppercase">Assembled messages</h4>
            <RenderedMessages result={{ messages: result.assembled.messages }} />
          </div>
          <div>
            <h4 className="text-xs font-semibold uppercase">Tool declarations</h4>
            <JsonView value={result.assembled.tools} label="Tool declarations" />
          </div>
          <div>
            <h4 className="text-xs font-semibold uppercase">Output</h4>
            <div className="rounded-md border border-slate-200 p-2">
              <Markdown source={result.output} />
            </div>
          </div>
          <p className="text-sm">
            Tokens: {result.usage.input_tokens} in / {result.usage.output_tokens} out
          </p>
        </div>
      )}
    </section>
  );
}
