'use client';

import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { Connection, ConnectionInvokeResult } from '@/lib/api/integrate-types';
import { ActionError, SelectField, TextField, useAction } from '../admin/ui';

type Parsed = { ok: true; value: unknown } | { ok: false; error: string };

function parseJson(text: string, label: string, optional: boolean): Parsed {
  if (text.trim() === '')
    return optional
      ? { ok: true, value: undefined }
      : { ok: false, error: `${label} is required.` };
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch {
    return { ok: false, error: `${label} must be valid JSON.` };
  }
}

/**
 * Governed invocation form. Database connections are read-only unless access_mode is read_write
 * and the caller also holds connection:write (enforced by the server).
 */
export function ConnectionConsole({
  org,
  connection,
  blockedReason,
}: {
  org: string;
  connection: Connection;
  /** When set, the form is disabled and this explains why (inactive connection, planned adapter). */
  blockedReason: string | null;
}) {
  const action = useAction();
  const [method, setMethod] = useState('GET');
  const [path, setPath] = useState('');
  const [query, setQuery] = useState('');
  const [body, setBody] = useState('');
  const [tool, setTool] = useState('');
  const [args, setArgs] = useState('');
  const [sql, setSql] = useState('');
  const [params, setParams] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [result, setResult] = useState<ConnectionInvokeResult | null>(null);
  const disabled = blockedReason !== null;
  const t = connection.connection_type;
  const readOnlyDb = t === 'database' && connection.access_mode !== 'read_write';

  async function run() {
    let payload: Record<string, unknown>;
    if (t === 'rest') {
      if (path.trim() === '') return setProblem('Path is required.');
      const q = parseJson(query, 'Query', true);
      const b = parseJson(body, 'Body', true);
      if (!q.ok) return setProblem(q.error);
      if (!b.ok) return setProblem(b.error);
      payload = {
        method,
        path: path.trim(),
        ...(q.value !== undefined ? { query: q.value } : {}),
        ...(b.value !== undefined ? { body: b.value } : {}),
      };
    } else if (t === 'mcp') {
      if (tool.trim() === '') return setProblem('Tool name is required.');
      const a = parseJson(args, 'Arguments', true);
      if (!a.ok) return setProblem(a.error);
      payload = { tool: tool.trim(), arguments: a.value ?? {} };
    } else if (t === 'database') {
      if (sql.trim() === '') return setProblem('SQL is required.');
      const p = parseJson(params, 'Parameters', true);
      if (!p.ok) return setProblem(p.error);
      payload = { sql, params: p.value ?? [] };
    } else {
      return setProblem('This connection type has no console.');
    }
    setProblem(null);
    setResult(null);
    const res = await action.run(() => endpoints.invokeConnection(org, connection.id, payload));
    if (res.ok) setResult(res.value);
  }

  const supported = t === 'rest' || t === 'mcp' || t === 'database';

  return (
    <section aria-labelledby="console-h" className="card space-y-4">
      <h2 id="console-h" className="text-lg font-semibold">
        Console
      </h2>
      {!supported ? (
        <p className="text-sm text-slate-600">
          Connections of type {t} are used by their provider or platform features and have no
          console.
        </p>
      ) : (
        <>
          {readOnlyDb && (
            <p
              role="note"
              className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
            >
              Read-only: statements run in a READ ONLY transaction with a timeout and a row cap.
              Writes are refused.
            </p>
          )}
          {t === 'database' && !readOnlyDb && (
            <p role="note" className="text-sm text-slate-600">
              This connection allows writes, which also require the connection:write permission.
            </p>
          )}
          {blockedReason && (
            <p role="note" className="rounded-md border border-slate-300 bg-slate-100 p-3 text-sm">
              {blockedReason}
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
            {t === 'rest' && (
              <>
                <div className="grid gap-3 sm:grid-cols-[8rem_1fr]">
                  <SelectField
                    label="Method"
                    value={method}
                    disabled={disabled}
                    onChange={setMethod}
                    options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => ({
                      value: m,
                      label: m,
                    }))}
                  />
                  <TextField
                    label="Path"
                    value={path}
                    onChange={setPath}
                    disabled={disabled}
                    monospace
                    placeholder="/items"
                  />
                </div>
                <TextField
                  label="Query (JSON object)"
                  value={query}
                  onChange={setQuery}
                  disabled={disabled}
                  multiline
                  monospace
                />
                <TextField
                  label="Body (JSON)"
                  value={body}
                  onChange={setBody}
                  disabled={disabled}
                  multiline
                  monospace
                />
              </>
            )}
            {t === 'mcp' && (
              <>
                <TextField
                  label="Tool"
                  value={tool}
                  onChange={setTool}
                  disabled={disabled}
                  monospace
                />
                <TextField
                  label="Arguments (JSON object)"
                  value={args}
                  onChange={setArgs}
                  disabled={disabled}
                  multiline
                  monospace
                />
              </>
            )}
            {t === 'database' && (
              <>
                <TextField
                  label="SQL"
                  value={sql}
                  onChange={setSql}
                  disabled={disabled}
                  multiline
                  monospace
                />
                <TextField
                  label="Parameters (JSON array)"
                  value={params}
                  onChange={setParams}
                  disabled={disabled}
                  multiline
                  monospace
                />
              </>
            )}
            <button type="submit" className="btn btn-primary" disabled={disabled || action.busy}>
              {action.busy ? 'Running...' : 'Run'}
            </button>
          </form>
          {problem && (
            <p role="alert" className="text-sm text-red-700">
              {problem}
            </p>
          )}
          <ActionError error={action.error} title="Invocation failed" />
          {result && (
            <div role="region" aria-label="Invocation result" className="space-y-2">
              {result.truncated && (
                <p
                  role="status"
                  className="rounded-md border border-amber-300 bg-amber-50 p-2 text-sm text-amber-900"
                >
                  Truncated: the result exceeded the server&apos;s size or row limit and is
                  incomplete.
                </p>
              )}
              <pre className="max-h-96 overflow-auto rounded-md border border-slate-300 bg-slate-100 p-3 text-xs">
                {JSON.stringify(result.result, null, 2)}
              </pre>
              <p className="text-sm text-slate-600">Completed in {result.duration_ms} ms.</p>
            </div>
          )}
        </>
      )}
    </section>
  );
}
