'use client';

import { useState } from 'react';
import type { Run, RunResult, RunStatus } from '@/lib/api/operate-types';
import { Markdown } from '@/lib/markdown';
import { failureInfo } from '@/lib/failure';
import {
  isOutcomeAnswered,
  STATUS_LABEL,
  STATUS_TONE,
  UNKNOWN_OUTCOME_LABEL,
} from '@/lib/run-utils';
import { Badge } from '../admin/ui';

const ENDED: readonly RunStatus[] = ['failed', 'cancelled', 'timed_out'];

/** Status chip: the text label carries the state (color is never the only signal). */
export function RunStatusChip({
  status,
  outcomeUnknown = false,
}: {
  status: RunStatus;
  /** When true the chip says the write outcome is unknown, never the plain status (R1). */
  outcomeUnknown?: boolean;
}) {
  if (outcomeUnknown) {
    // An ended run reads only as unknown (never a plain Cancelled/Timed out). A parent that has not
    // ended can be flagged because of a child, so its real status stays visible beside the marker.
    const live = !ENDED.includes(status);
    const real = STATUS_LABEL[status] ?? status;
    return (
      <span
        aria-label={`Run status: ${UNKNOWN_OUTCOME_LABEL}${live ? `, ${real}` : ''}`}
        className="inline-flex flex-wrap items-center gap-1"
      >
        <Badge tone="bad">{UNKNOWN_OUTCOME_LABEL}</Badge>
        {live && <Badge tone={STATUS_TONE[status] ?? 'neutral'}>{real}</Badge>}
      </span>
    );
  }
  return (
    <span aria-label={`Run status: ${STATUS_LABEL[status] ?? status}`}>
      <Badge tone={STATUS_TONE[status] ?? 'neutral'}>{STATUS_LABEL[status] ?? status}</Badge>
    </span>
  );
}

/** Copies a value to the clipboard; reports failure instead of swallowing it. */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState('copied');
    } catch {
      setState('failed');
    }
  }
  return (
    <button
      type="button"
      className="btn ml-2 px-2 py-0.5 text-xs"
      aria-label={`Copy ${label}`}
      onClick={() => void copy()}
    >
      {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}
    </button>
  );
}

/** Failure Details: error class, explanation, retryable flag, suggested action. */
export function FailurePanel({ run }: { run: Run }) {
  const base = failureInfo(run.error_class);
  if (!base) return null;
  const answered = isOutcomeAnswered(run);
  // R1: a run with an open or answered unknown-outcome question is never described as a plain failure.
  const info = run.outcome_unknown
    ? {
        title: UNKNOWN_OUTCOME_LABEL,
        explanation:
          'A write in this run, or in a run it started, may have been delivered before that run ended, so the platform cannot tell whether it happened.',
        retryable: false,
        action:
          'Check the target system, then record the outcome in the pending approval (in Approvals, or on the run detail page): mark succeeded if the write happened, or fail if it did not.',
      }
    : answered
      ? {
          title: 'Write outcome recorded',
          explanation:
            'The run ended while a write outcome was unknown. An operator has since recorded it; the message below names the verdict.',
          retryable: false,
          action: 'No action needed.',
        }
      : base;
  return (
    <section
      aria-label="Failure details"
      className="card space-y-2 border-red-300 bg-red-50"
      role="region"
    >
      <h3 className="text-sm font-semibold text-red-900">{info.title}</h3>
      <dl className="grid gap-1 text-sm sm:grid-cols-[10rem_1fr]">
        <dt className="font-medium">Error class</dt>
        <dd>
          <code className="font-mono text-xs">{run.error_class}</code>
        </dd>
        <dt className="font-medium">What happened</dt>
        <dd>{info.explanation}</dd>
        <dt className="font-medium">Retryable</dt>
        <dd>{info.retryable ? 'Yes, transient failures are retried automatically' : 'No'}</dd>
        <dt className="font-medium">Suggested action</dt>
        <dd>{info.action}</dd>
        <dt className="font-medium">Attempts</dt>
        <dd>{run.attempt_count}</dd>
        {run.error_message && (
          <>
            <dt className="font-medium">Message</dt>
            <dd className="break-words">{run.error_message}</dd>
          </>
        )}
      </dl>
    </section>
  );
}

/** Server message for runs whose unknown-write question is open or answered (list and detail). */
export function RunOutcomeNote({ run }: { run: Run }) {
  if (!run.error_message || !(run.outcome_unknown || isOutcomeAnswered(run))) return null;
  return <p className="mt-1 max-w-md text-xs break-words text-slate-700">{run.error_message}</p>;
}

export function UsageSummary({ run }: { run: Run }) {
  const u = run.usage;
  const rows: [string, number | undefined][] = [
    ['Input tokens', u?.input_tokens],
    ['Output tokens', u?.output_tokens],
    ['Cached tokens', u?.cached_tokens],
    ['Model calls', u?.model_calls],
    ['Tool calls', u?.tool_calls],
  ];
  return (
    <section aria-label="Usage summary" className="card">
      <h3 className="text-sm font-semibold">Usage</h3>
      <dl className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-5">
        {rows.map(([label, v]) => (
          <div key={label}>
            <dt className="text-xs text-slate-600">{label}</dt>
            <dd className="text-lg font-semibold">{v ?? 0}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** Final output through the safe markdown renderer (never raw HTML). */
export function ResultView({ result }: { result: RunResult }) {
  const out = result.text ?? result.output;
  const text =
    typeof out === 'string' ? out : out === undefined ? '' : JSON.stringify(out, null, 2);
  return (
    <section aria-label="Run result" className="card space-y-2">
      <h3 className="text-sm font-semibold">Result</h3>
      {result.redacted && (
        <p className="text-xs text-slate-600">
          Output is redacted according to the content-capture mode.
        </p>
      )}
      {text ? (
        typeof out === 'string' ? (
          <Markdown source={text} />
        ) : (
          <pre
            tabIndex={0}
            className="max-h-96 overflow-auto rounded bg-slate-100 p-2 font-mono text-xs"
          >
            {text}
          </pre>
        )
      ) : (
        <p className="text-sm text-slate-600">No output recorded.</p>
      )}
    </section>
  );
}
