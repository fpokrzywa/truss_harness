'use client';

import Link from 'next/link';
import { useId, useState, type FormEvent } from 'react';
import type { AppRequestOut, DraftOut } from '@/lib/api/appgen-types';
import { endpoints } from '@/lib/api/endpoints';
import { useAppRequest, useRefreshAppRequests } from '@/lib/appgen-hooks';
import { useCan } from '@/lib/hooks';
import { generateHref, solutionHref } from '@/lib/scope';
import { ConfirmDialog } from '../ConfirmDialog';
import {
  ActionError,
  EmptyState,
  formatDate,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { ModelSelect } from '../build/pickers';
import { StatusBadge } from '../integrate/Maturity';
import { useProjectQuery } from '../releases/ProjectScope';
import { useOrgParam } from '../security/useOrg';
import { DraftEditor, errorCount, isFixedPath, RevisionHistory } from './DraftEditor';

// The same checks as the evaluation contract of a new solution version.
const DECIMAL = /^[0-9]{1,12}(\.[0-9]{1,12})?$/;
const POSITIVE_INT = /^[1-9][0-9]*$/;

const WORKING_TEXT: Record<string, string> = {
  queued: 'Queued...',
  drafting: 'Drafting...',
  accepting: 'Accepting...',
};

interface Scope {
  org: string;
  /** The project slugs from `?ws=&proj=`; empty when the link names none. */
  ws: string;
  proj: string;
}

/** One app request: its status, its draft revisions, accept, discard and retry. */
export function AppRequestView({ id }: { id: string }) {
  const org = useOrgParam();
  const { ws, proj } = useProjectQuery();
  const { query, polling, timedOut } = useAppRequest(org, id);
  return (
    <div className="space-y-6">
      <QueryGate query={query} noun="app request">
        {(r) => (
          <RequestBody
            scope={{ org, ws, proj }}
            request={r}
            polling={polling}
            timedOut={timedOut}
            refetch={() => query.refetch()}
          />
        )}
      </QueryGate>
    </div>
  );
}

const latestOf = (revisions: DraftOut[]) =>
  revisions.reduce<DraftOut | undefined>(
    (best, r) => (best === undefined || r.revision_no > best.revision_no ? r : best),
    undefined,
  );

function RequestBody({
  scope,
  request,
  polling,
  timedOut,
  refetch,
}: {
  scope: Scope;
  request: AppRequestOut;
  polling: boolean;
  timedOut: boolean;
  refetch: () => Promise<unknown>;
}) {
  const { org, ws, proj } = scope;
  const { can } = useCan(org);
  const refresh = useRefreshAppRequests(org);
  const canWrite = can('appgen:request:write');
  const canAccept = can('appgen:draft:accept');
  const [shownNo, setShownNo] = useState<number | null>(null);
  const discard = useAction();
  const retry = useAction();
  const [confirming, setConfirming] = useState(false);
  const [dirty, setDirty] = useState(false);

  const latest = latestOf(request.revisions);
  const shown = request.revisions.find((r) => r.revision_no === shownNo) ?? latest;
  const status = request.status;
  const showAccept =
    canAccept &&
    (status === 'drafted' || (status === 'accepting' && request.status_reason !== null));

  async function confirmDiscard() {
    const res = await discard.run(() => endpoints.discardAppRequest(org, request.id));
    setConfirming(false);
    if (res.ok) await refresh();
  }

  async function runRetry() {
    const res = await retry.run(() => endpoints.retryAppRequest(org, request.id));
    if (res.ok) await refresh();
  }

  return (
    <>
      <PageHeader
        title="App request"
        description="A drafted app over a project's Databricks or Snowflake tables. Review the draft, edit it, and accept it as a solution."
        actions={
          canWrite && (
            <>
              {status === 'failed' && (
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={retry.busy}
                  onClick={() => void runRetry()}
                >
                  {retry.busy ? 'Working...' : 'Retry'}
                </button>
              )}
              {(status === 'drafted' || status === 'failed') && (
                <button type="button" className="btn" onClick={() => setConfirming(true)}>
                  Discard
                </button>
              )}
            </>
          )
        }
      />
      <p className="text-sm">
        <Link className="text-blue-800 underline" href={generateHref(org, ws, proj)}>
          Back to the solution generator
        </Link>
      </p>
      <ActionError error={retry.error} title="Could not retry the request" />
      <ActionError error={discard.error} title="Could not discard the request" />
      <p role="status" aria-live="polite" className="text-sm text-slate-700">
        {polling ? WORKING_TEXT[status] : timedOut ? 'Still working; refresh to check again' : null}
      </p>

      <section aria-label="Request" className="card">
        <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
          <dt className="text-slate-600">Status</dt>
          <dd>
            <StatusBadge status={status} />
          </dd>
          {request.status_reason !== null && (
            <>
              <dt className="text-slate-600">Reason</dt>
              <dd>{request.status_reason}</dd>
            </>
          )}
          <dt className="text-slate-600">Brief</dt>
          <dd className="whitespace-pre-wrap">{request.brief}</dd>
          <dt className="text-slate-600">Tables</dt>
          <dd>
            <ul className="font-mono text-xs">
              {request.tables.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          </dd>
          <dt className="text-slate-600">Created</dt>
          <dd>{formatDate(request.created_at)}</dd>
        </dl>
      </section>

      {status === 'accepted' && <Accepted scope={scope} request={request} />}

      {latest === undefined || shown === undefined ? (
        <div className="card p-0">
          <EmptyState>No draft revisions yet.</EmptyState>
        </div>
      ) : (
        <>
          <RevisionHistory
            revisions={request.revisions}
            shown={shown.revision_no}
            onShow={setShownNo}
          />
          <DraftEditor
            key={shown.revision_no}
            org={org}
            request={request}
            revision={shown}
            latestNo={latest.revision_no}
            editable={status === 'drafted' && canWrite && shown.revision_no === latest.revision_no}
            onSaved={async () => {
              setShownNo(null);
              await refresh();
            }}
            onReload={() => void refetch()}
            onDirtyChange={setDirty}
          />
        </>
      )}

      {showAccept && <AcceptForm scope={scope} request={request} latest={latest} unsaved={dirty} />}

      <ConfirmDialog
        open={confirming}
        title="Discard request?"
        confirmLabel="Discard request"
        busy={discard.busy}
        onConfirm={() => void confirmDiscard()}
        onCancel={() => setConfirming(false)}
      >
        <p>The request and its drafts stay readable, but it can no longer be edited or accepted.</p>
      </ConfirmDialog>
    </>
  );
}

function Accepted({ scope, request }: { scope: Scope; request: AppRequestOut }) {
  const { org, ws, proj } = scope;
  const solutionId = request.accept_progress.solution_id;
  return (
    <section aria-label="Accepted" className="card space-y-1 text-sm">
      <h2 className="text-base font-semibold">Accepted</h2>
      {ws && proj && typeof solutionId === 'string' ? (
        <p>
          <Link className="text-blue-800 underline" href={solutionHref(org, solutionId, ws, proj)}>
            Open the solution
          </Link>
        </p>
      ) : (
        <p>
          Solution version:{' '}
          <code className="font-mono text-xs">{request.solution_version_id ?? '-'}</code>
        </p>
      )}
    </section>
  );
}

function AcceptForm({
  scope,
  request,
  latest,
  unsaved,
}: {
  scope: Scope;
  request: AppRequestOut;
  latest: DraftOut | undefined;
  /** The draft editor holds edits that are not saved as a revision yet. */
  unsaved: boolean;
}) {
  const { org, ws, proj } = scope;
  const refresh = useRefreshAppRequests(org);
  const action = useAction();
  const [name, setName] = useState('');
  const [modelId, setModelId] = useState('');
  const [minAccuracy, setMinAccuracy] = useState('');
  const [maxLatency, setMaxLatency] = useState('');
  const [maxCost, setMaxCost] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const reasonId = useId();
  const unsavedId = useId();

  const resuming = request.status === 'accepting';
  const revisionNo = resuming ? request.accepted_revision_no : (latest?.revision_no ?? null);
  const accepted = request.revisions.find((r) => r.revision_no === revisionNo);
  const errorsInDraft = errorCount(accepted);
  const fixedErrors = (accepted?.validation ?? []).some(
    (d) => d.severity === 'error' && isFixedPath(d.path),
  );
  const blocked = errorsInDraft > 0 || revisionNo === null || unsaved;
  const describedBy = [errorsInDraft > 0 ? reasonId : null, unsaved ? unsavedId : null].filter(
    Boolean,
  );
  const label = resuming ? 'Resume accept' : 'Accept';

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (revisionNo === null) return;
    const next: Record<string, string> = {};
    const trimmed = name.trim();
    if (trimmed === '' || trimmed.length > 200) next.name = 'Name must be 1 to 200 characters.';
    if (modelId === '') next.model = 'Choose a runtime model.';
    const acc = minAccuracy.trim();
    if (!DECIMAL.test(acc) || Number(acc) > 1)
      next.minAccuracy = 'Enter a decimal between 0 and 1.';
    if (!POSITIVE_INT.test(maxLatency.trim()))
      next.maxLatency = 'Enter a positive whole number of milliseconds.';
    if (!DECIMAL.test(maxCost.trim())) next.maxCost = 'Enter a decimal amount in USD.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const res = await action.run(() =>
      endpoints.acceptAppRequest(org, request.id, {
        revision_no: revisionNo,
        solution_name: trimmed,
        model_id: modelId,
        thresholds: {
          min_accuracy: acc,
          max_p95_latency_ms: Number(maxLatency.trim()),
          max_total_cost_usd: maxCost.trim(),
        },
      }),
    );
    if (res.ok) await refresh();
  }

  return (
    <form
      onSubmit={(e) => void submit(e)}
      aria-label="Accept draft"
      className="card max-w-3xl space-y-4"
      noValidate
    >
      <h2 className="text-base font-semibold">
        {resuming ? 'Resume accept' : 'Accept draft'}
        {revisionNo !== null && ` r${revisionNo}`}
      </h2>
      <p className="text-sm text-slate-700">
        Accepting publishes the draft&apos;s tools and agent, adds its evaluation cases as a
        dataset, and creates a solution version with these thresholds.
      </p>
      <TextField
        label="Solution name"
        value={name}
        onChange={setName}
        maxLength={200}
        error={errors.name}
      />
      <div>
        {/* The project's model list; without `?ws=&proj=` the organization's. */}
        <ModelSelect
          org={org}
          workspace={ws || undefined}
          project={proj || undefined}
          label="Runtime model"
          value={modelId}
          onChange={setModelId}
          emptyLabel="Select..."
        />
        {errors.model && <p className="mt-1 text-sm text-red-700">{errors.model}</p>}
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <TextField
          label="Min accuracy"
          value={minAccuracy}
          onChange={setMinAccuracy}
          help="Between 0 and 1."
          error={errors.minAccuracy}
        />
        <TextField
          label="Max p95 latency (ms)"
          value={maxLatency}
          onChange={setMaxLatency}
          error={errors.maxLatency}
        />
        <TextField
          label="Max total cost (USD)"
          value={maxCost}
          onChange={setMaxCost}
          error={errors.maxCost}
        />
      </div>
      <ActionError error={action.error} title="Could not accept the draft" />
      <div className="flex flex-wrap items-center justify-end gap-3">
        {errorsInDraft > 0 && (
          <p id={reasonId} className="text-sm text-red-800">
            Fix the {errorsInDraft} {errorsInDraft === 1 ? 'error' : 'errors'} in this draft before
            accepting.
            {fixedErrors &&
              ' Some of these errors are on names this editor cannot change; discard this request and generate a new one.'}
          </p>
        )}
        {unsaved && (
          <p id={unsavedId} className="text-sm text-red-800">
            Save your draft edits as a new revision before accepting; Accept uses the latest saved
            revision.
          </p>
        )}
        <button
          type="submit"
          className="btn btn-primary"
          disabled={action.busy || blocked}
          aria-describedby={describedBy.join(' ') || undefined}
        >
          {action.busy ? 'Working...' : label}
        </button>
      </div>
    </form>
  );
}
