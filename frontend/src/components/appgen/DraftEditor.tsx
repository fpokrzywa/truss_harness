'use client';

import { useId, useState, type FormEvent, type ReactNode } from 'react';
import {
  PARAMETER_TYPES,
  type AppRequestOut,
  type Draft,
  type DraftDiagnostic,
  type DraftOut,
  type ParameterType,
} from '@/lib/api/appgen-types';
import type { CaseAssertion } from '@/lib/api/evaluation-types';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import { ActionError, formatDate, useAction } from '../admin/ui';

const POSITIVE_INT = /^[1-9][0-9]*$/;

/** A field the editor renders, and the diagnostic paths it shows: its own, or all below it. */
interface Anchor {
  path: string;
  prefix?: boolean;
}

const matches = (a: Anchor, path: string) =>
  path === a.path || (a.prefix === true && path.startsWith(`${a.path}.`));

/** Every field anchor of a draft, as the editor renders them. */
function anchorsOf(draft: Draft): Anchor[] {
  const out: Anchor[] = [{ path: 'agent.instructions' }];
  draft.tools.forEach((t, i) => {
    const p = `tools.${i}`;
    out.push({ path: `${p}.description` }, { path: `${p}.sql` }, { path: `${p}.max_rows` });
    out.push({ path: `${p}.parameters` });
    t.parameters.forEach((_, j) =>
      out.push(
        { path: `${p}.parameters.${j}.name` },
        { path: `${p}.parameters.${j}.type` },
        { path: `${p}.parameters.${j}.required` },
      ),
    );
  });
  draft.screens.forEach((s, i) => {
    out.push({ path: `screens.${i}.title` });
    if (s.kind === 'chat') out.push({ path: `screens.${i}.placeholder` });
    else out.push({ path: `screens.${i}.tool` }, { path: `screens.${i}.inputs`, prefix: true });
  });
  draft.evaluation_cases.forEach((_, i) =>
    out.push(
      { path: `evaluation_cases.${i}.input.message` },
      { path: `evaluation_cases.${i}.assertions`, prefix: true },
    ),
  );
  return out;
}

export const errorCount = (rev: DraftOut | undefined) =>
  rev?.validation.filter((d) => d.severity === 'error').length ?? 0;

const warningCount = (rev: DraftOut) =>
  rev.validation.filter((d) => d.severity === 'warning').length;

function Diagnostics({ id, items }: { id?: string; items: DraftDiagnostic[] }) {
  if (items.length === 0) return null;
  return (
    <ul id={id} className="mt-1 space-y-1 text-sm">
      {items.map((d, i) => (
        <li
          key={`${d.path}-${d.code}-${i}`}
          className={d.severity === 'error' ? 'text-red-800' : 'text-amber-900'}
        >
          <span className="font-semibold">{d.severity === 'error' ? 'Error' : 'Warning'}</span>{' '}
          <code className="font-mono text-xs">{d.code}</code>: {d.message}
        </li>
      ))}
    </ul>
  );
}

/** A labelled control with the diagnostics of its path and a client error, wired through aria-describedby. */
function Field({
  label,
  diagnostics,
  error,
  children,
}: {
  label: string;
  diagnostics: DraftDiagnostic[];
  error?: string;
  children: (props: { id: string; 'aria-describedby'?: string }) => ReactNode;
}) {
  const id = useId();
  const described = [
    diagnostics.length > 0 ? `${id}-diag` : null,
    error ? `${id}-err` : null,
  ].filter(Boolean);
  return (
    <div>
      <label htmlFor={id} className="label">
        {label}
      </label>
      {children({ id, 'aria-describedby': described.join(' ') || undefined })}
      <Diagnostics id={`${id}-diag`} items={diagnostics} />
      {error && (
        <p id={`${id}-err`} className="mt-1 text-sm text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}

const copy = (d: Draft): Draft => JSON.parse(JSON.stringify(d)) as Draft;

/**
 * One revision of a draft. Edits the fields of its tools, screens and cases (and adds or removes
 * tool parameters); `editable` is false for an older revision, another status or a reader.
 */
export function DraftEditor({
  org,
  request,
  revision,
  latestNo,
  editable,
  onSaved,
  onReload,
}: {
  org: string;
  request: AppRequestOut;
  revision: DraftOut;
  latestNo: number;
  editable: boolean;
  onSaved: () => Promise<unknown>;
  onReload: () => void;
}) {
  const action = useAction();
  const [draft, setDraft] = useState(() => copy(revision.draft));
  const [maxRows, setMaxRows] = useState(() => revision.draft.tools.map((t) => String(t.max_rows)));
  const [inputs, setInputs] = useState(() =>
    revision.draft.screens.map((s) => (s.kind === 'table' ? s.inputs.join(', ') : '')),
  );
  const [assertions, setAssertions] = useState(() =>
    revision.draft.evaluation_cases.map((c) => JSON.stringify(c.assertions, null, 2)),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const readOnly = !editable;

  const anchors = anchorsOf(revision.draft);
  const at = (path: string, prefix = false) =>
    revision.validation.filter((d) => matches({ path, prefix }, d.path));
  const unplaced = revision.validation.filter((d) => !anchors.some((a) => matches(a, d.path)));

  const update = (fn: (d: Draft) => void) =>
    setDraft((d) => {
      const next = copy(d);
      fn(next);
      return next;
    });
  const setAt = (list: string[], i: number, v: string) => list.map((x, j) => (j === i ? v : x));

  async function save(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    const out = copy(draft);
    out.tools.forEach((t, i) => {
      const rows = (maxRows[i] ?? '').trim();
      if (!POSITIVE_INT.test(rows)) next[`tool-${i}-rows`] = 'Enter a positive whole number.';
      else t.max_rows = Number(rows);
    });
    out.screens.forEach((s, i) => {
      if (s.kind === 'table')
        s.inputs = (inputs[i] ?? '')
          .split(',')
          .map((x) => x.trim())
          .filter((x) => x !== '');
    });
    out.evaluation_cases.forEach((c, i) => {
      try {
        const parsed: unknown = JSON.parse(assertions[i] ?? '');
        if (!Array.isArray(parsed)) throw new Error('not a list');
        c.assertions = parsed as CaseAssertion[];
      } catch {
        next[`case-${i}-assertions`] = 'Enter the assertions as a JSON list.';
      }
    });
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    const res = await action.run(() =>
      endpoints.reviseAppDraft(org, request.id, { base_revision_no: latestNo, draft: out }),
    );
    if (res.ok) await onSaved();
  }

  const stale = action.error instanceof ApiError && action.error.code === 'stale_revision';

  return (
    <form
      onSubmit={(e) => void save(e)}
      aria-label={`Draft revision ${revision.revision_no}`}
      className="card space-y-6"
      noValidate
    >
      <div>
        <h2 className="text-base font-semibold">Draft r{revision.revision_no}</h2>
        {!editable && (
          <p className="mt-1 text-sm text-slate-600">
            This revision is read-only.
            {revision.revision_no !== latestNo && ' Choose the latest revision to edit the draft.'}
          </p>
        )}
      </div>
      {unplaced.length > 0 && (
        <section aria-label="Draft diagnostics" className="rounded-md border border-slate-200 p-3">
          <h3 className="text-sm font-semibold">Draft diagnostics</h3>
          <Diagnostics items={unplaced} />
        </section>
      )}

      <fieldset className="space-y-3">
        <legend className="label">Agent {draft.agent.name}</legend>
        <Field label="Agent instructions" diagnostics={at('agent.instructions')}>
          {(p) => (
            <textarea
              {...p}
              className={`input${readOnly ? ' input-readonly' : ''}`}
              rows={6}
              value={draft.agent.instructions}
              readOnly={readOnly}
              onChange={(e) =>
                update((d) => {
                  d.agent.instructions = e.target.value;
                })
              }
            />
          )}
        </Field>
      </fieldset>

      {draft.tools.map((tool, i) => (
        <fieldset key={i} className="space-y-3 rounded-md border border-slate-200 p-3">
          <legend className="px-1 text-sm font-semibold">
            Tool {i + 1}: <span className="font-mono">{tool.name}</span>
          </legend>
          <Field label="Description" diagnostics={at(`tools.${i}.description`)}>
            {(p) => (
              <input
                {...p}
                className={`input${readOnly ? ' input-readonly' : ''}`}
                value={tool.description}
                readOnly={readOnly}
                onChange={(e) =>
                  update((d) => {
                    d.tools[i]!.description = e.target.value;
                  })
                }
              />
            )}
          </Field>
          <Field label="SQL" diagnostics={at(`tools.${i}.sql`)}>
            {(p) => (
              <textarea
                {...p}
                className={`input font-mono${readOnly ? ' input-readonly' : ''}`}
                rows={5}
                value={tool.sql}
                readOnly={readOnly}
                onChange={(e) =>
                  update((d) => {
                    d.tools[i]!.sql = e.target.value;
                  })
                }
              />
            )}
          </Field>
          <Field
            label="Max rows"
            diagnostics={at(`tools.${i}.max_rows`)}
            error={errors[`tool-${i}-rows`]}
          >
            {(p) => (
              <input
                {...p}
                className={`input max-w-32${readOnly ? ' input-readonly' : ''}`}
                inputMode="numeric"
                value={maxRows[i] ?? ''}
                readOnly={readOnly}
                onChange={(e) => setMaxRows((rows) => setAt(rows, i, e.target.value))}
              />
            )}
          </Field>
          <fieldset className="space-y-2">
            <legend className="label">Parameters of {tool.name}</legend>
            <Diagnostics items={at(`tools.${i}.parameters`)} />
            {tool.parameters.length === 0 && (
              <p className="text-sm text-slate-600">No parameters.</p>
            )}
            {tool.parameters.map((param, j) => {
              const pp = `tools.${i}.parameters.${j}`;
              return (
                <div
                  key={j}
                  className="grid items-start gap-2 rounded-md border border-slate-100 p-2 sm:grid-cols-[1fr_10rem_auto_auto]"
                >
                  <Field label={`Parameter ${j + 1} name`} diagnostics={at(`${pp}.name`)}>
                    {(p) => (
                      <input
                        {...p}
                        className={`input font-mono${readOnly ? ' input-readonly' : ''}`}
                        value={param.name}
                        readOnly={readOnly}
                        onChange={(e) =>
                          update((d) => {
                            d.tools[i]!.parameters[j]!.name = e.target.value;
                          })
                        }
                      />
                    )}
                  </Field>
                  <Field label={`Parameter ${j + 1} type`} diagnostics={at(`${pp}.type`)}>
                    {(p) => (
                      <select
                        {...p}
                        className="input"
                        value={param.type}
                        disabled={readOnly}
                        onChange={(e) =>
                          update((d) => {
                            d.tools[i]!.parameters[j]!.type = e.target.value as ParameterType;
                          })
                        }
                      >
                        {PARAMETER_TYPES.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                    )}
                  </Field>
                  <div className="pt-6">
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={param.required}
                        disabled={readOnly}
                        onChange={(e) =>
                          update((d) => {
                            d.tools[i]!.parameters[j]!.required = e.target.checked;
                          })
                        }
                      />
                      Parameter {j + 1} required
                    </label>
                    <Diagnostics items={at(`${pp}.required`)} />
                  </div>
                  {editable && (
                    <div className="pt-6">
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          update((d) => {
                            d.tools[i]!.parameters.splice(j, 1);
                          })
                        }
                      >
                        Remove parameter {j + 1}
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
            {editable && (
              <button
                type="button"
                className="btn"
                onClick={() =>
                  update((d) => {
                    d.tools[i]!.parameters.push({ name: '', type: 'string', required: true });
                  })
                }
              >
                Add parameter <span className="sr-only">to {tool.name}</span>
              </button>
            )}
          </fieldset>
        </fieldset>
      ))}

      {draft.screens.map((s, i) => (
        <fieldset key={i} className="space-y-3 rounded-md border border-slate-200 p-3">
          <legend className="px-1 text-sm font-semibold">
            Screen {i + 1} ({s.kind})
          </legend>
          <Field label="Title" diagnostics={at(`screens.${i}.title`)}>
            {(p) => (
              <input
                {...p}
                className={`input${readOnly ? ' input-readonly' : ''}`}
                value={s.title}
                readOnly={readOnly}
                onChange={(e) =>
                  update((d) => {
                    d.screens[i]!.title = e.target.value;
                  })
                }
              />
            )}
          </Field>
          {s.kind === 'chat' ? (
            <Field label="Placeholder" diagnostics={at(`screens.${i}.placeholder`)}>
              {(p) => (
                <input
                  {...p}
                  className={`input${readOnly ? ' input-readonly' : ''}`}
                  value={s.placeholder ?? ''}
                  readOnly={readOnly}
                  onChange={(e) =>
                    update((d) => {
                      const screen = d.screens[i];
                      if (screen?.kind === 'chat') screen.placeholder = e.target.value;
                    })
                  }
                />
              )}
            </Field>
          ) : (
            <>
              <Field label="Tool" diagnostics={at(`screens.${i}.tool`)}>
                {(p) => (
                  <select
                    {...p}
                    className="input"
                    value={s.tool}
                    disabled={readOnly}
                    onChange={(e) =>
                      update((d) => {
                        const screen = d.screens[i];
                        if (screen?.kind === 'table') screen.tool = e.target.value;
                      })
                    }
                  >
                    {!draft.tools.some((t) => t.name === s.tool) && (
                      <option value={s.tool}>{s.tool}</option>
                    )}
                    {draft.tools.map((t, k) => (
                      <option key={k} value={t.name}>
                        {t.name}
                      </option>
                    ))}
                  </select>
                )}
              </Field>
              <Field label="Inputs" diagnostics={at(`screens.${i}.inputs`, true)}>
                {(p) => (
                  <input
                    {...p}
                    className={`input font-mono${readOnly ? ' input-readonly' : ''}`}
                    value={inputs[i] ?? ''}
                    readOnly={readOnly}
                    placeholder="Parameter names, comma-separated"
                    onChange={(e) => setInputs((list) => setAt(list, i, e.target.value))}
                  />
                )}
              </Field>
            </>
          )}
        </fieldset>
      ))}

      {draft.evaluation_cases.map((c, i) => (
        <fieldset key={i} className="space-y-3 rounded-md border border-slate-200 p-3">
          <legend className="px-1 text-sm font-semibold">
            Evaluation case {i + 1}: <span className="font-mono">{c.case_id}</span>
          </legend>
          <Field label="Message" diagnostics={at(`evaluation_cases.${i}.input.message`)}>
            {(p) => (
              <textarea
                {...p}
                className={`input${readOnly ? ' input-readonly' : ''}`}
                rows={2}
                value={c.input.message}
                readOnly={readOnly}
                onChange={(e) =>
                  update((d) => {
                    d.evaluation_cases[i]!.input.message = e.target.value;
                  })
                }
              />
            )}
          </Field>
          <Field
            label="Assertions (JSON)"
            diagnostics={at(`evaluation_cases.${i}.assertions`, true)}
            error={errors[`case-${i}-assertions`]}
          >
            {(p) => (
              <textarea
                {...p}
                className={`input font-mono${readOnly ? ' input-readonly' : ''}`}
                rows={4}
                value={assertions[i] ?? ''}
                readOnly={readOnly}
                onChange={(e) => setAssertions((list) => setAt(list, i, e.target.value))}
              />
            )}
          </Field>
        </fieldset>
      ))}

      {stale ? (
        <div
          role="alert"
          className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
        >
          <p>
            This draft changed elsewhere since you opened it. Reload to see the latest revision,
            then make your edits again.
          </p>
          <button type="button" className="btn mt-2" onClick={onReload}>
            Reload
          </button>
        </div>
      ) : (
        <ActionError error={action.error} title="Could not save the draft" />
      )}
      {editable && (
        <div className="flex justify-end">
          <button type="submit" className="btn btn-primary" disabled={action.busy}>
            {action.busy ? 'Working...' : 'Save as new revision'}
          </button>
        </div>
      )}
    </form>
  );
}

/** Every revision of the request; choosing one shows it in the editor. */
export function RevisionHistory({
  revisions,
  shown,
  onShow,
}: {
  revisions: DraftOut[];
  shown: number;
  onShow: (revisionNo: number) => void;
}) {
  return (
    <section aria-label="Revision history" className="card overflow-x-auto p-0">
      <table className="w-full">
        <caption className="px-4 pt-4 text-left text-base font-semibold">Revision history</caption>
        <thead className="border-b border-slate-200">
          <tr>
            {['Revision', 'Source', 'Created', 'Errors', 'Warnings'].map((h) => (
              <th key={h} scope="col" className="th">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {[...revisions]
            .sort((a, b) => b.revision_no - a.revision_no)
            .map((r) => (
              <tr key={r.revision_no}>
                <td className="td">
                  <button
                    type="button"
                    className="text-blue-800 underline"
                    aria-current={r.revision_no === shown ? 'true' : undefined}
                    onClick={() => onShow(r.revision_no)}
                  >
                    Show r{r.revision_no}
                  </button>
                </td>
                <td className="td">{r.source}</td>
                <td className="td">{formatDate(r.created_at)}</td>
                <td className="td">{errorCount(r)}</td>
                <td className="td">{warningCount(r)}</td>
              </tr>
            ))}
        </tbody>
      </table>
    </section>
  );
}
