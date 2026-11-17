'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { endpoints } from '@/lib/api/endpoints';
import type { Approval, ApprovalDecision, ApprovalSummary } from '@/lib/api/operate-types';
import { ApiError } from '@/lib/api/errors';
import { useSession } from '@/lib/hooks';
import { useApproval, useApprovals, useRun } from '@/lib/operate-hooks';
import {
  canRetryUnknownWrite,
  countdownText,
  isInitiator,
  principalLabel,
  secondsUntil,
} from '@/lib/run-utils';
import { orgHref } from '@/lib/scope';
import {
  ActionError,
  Badge,
  EmptyState,
  formatDate,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { ConfirmDialog } from '../ConfirmDialog';
import { QueryError } from '../QueryError';
import { useOrgParam } from '../security/useOrg';

type Tab = 'pending' | 'decided' | 'expired';
const TABS: { id: Tab; label: string }[] = [
  { id: 'pending', label: 'Pending' },
  { id: 'decided', label: 'Decided' },
  { id: 'expired', label: 'Expired' },
];

const REASON_LABEL: Record<string, string> = {
  tool_side_effect: 'Tool side effect',
  approval_rule: 'Approval rule',
  orchestration_node: 'Orchestration approval node',
  indeterminate_side_effect: 'Indeterminate side effect',
};

export function ApprovalsView() {
  const org = useOrgParam();
  const [tab, setTab] = useState<Tab>('pending');
  const [selected, setSelected] = useState<string | undefined>();
  const query = useApprovals(org, tab);

  const rows = query.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Approvals"
        description="Human-in-the-loop decisions for runs. You cannot decide approvals on runs you started."
      />
      <div
        role="tablist"
        aria-label="Approval status"
        className="flex gap-1 border-b border-slate-200"
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`apr-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls="apr-panel"
            className={`px-3 py-2 text-sm font-medium ${
              tab === t.id ? 'border-b-2 border-blue-800 text-blue-900' : 'text-slate-600'
            }`}
            onClick={() => {
              setTab(t.id);
              setSelected(undefined);
            }}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id="apr-panel"
        aria-labelledby={`apr-tab-${tab}`}
        className="grid gap-4 lg:grid-cols-[2fr_3fr]"
      >
        <div>
          {query.isPending ? (
            <p role="status" className="text-sm text-slate-600">
              Loading approvals...
            </p>
          ) : query.isError ? (
            <QueryError error={query.error} />
          ) : rows.length === 0 ? (
            <div className="card p-0">
              <EmptyState>No {tab} approvals.</EmptyState>
            </div>
          ) : (
            <ul aria-label={`${tab} approvals`} className="card divide-y divide-slate-100 p-0">
              {rows.map((a) => (
                <ApprovalRow
                  key={a.id}
                  approval={a}
                  active={a.id === selected}
                  onSelect={() => setSelected(a.id)}
                />
              ))}
            </ul>
          )}
          {query.hasNextPage && (
            <button
              type="button"
              className="btn mt-3"
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              {query.isFetchingNextPage ? 'Loading...' : 'Load more'}
            </button>
          )}
        </div>
        <div>
          {selected ? (
            <ApprovalDetail key={selected} org={org} id={selected} />
          ) : (
            <p className="text-sm text-slate-600">Select an approval to review its arguments.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function ApprovalRow({
  approval,
  active,
  onSelect,
}: {
  approval: ApprovalSummary;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <li className={`flex items-center gap-3 p-3 text-sm ${active ? 'bg-blue-50' : ''}`}>
      <div className="min-w-0 flex-1">
        <div className="font-medium">
          {approval.tool?.key ?? REASON_LABEL[approval.reason] ?? approval.reason}
        </div>
        <div className="text-xs text-slate-600">
          {REASON_LABEL[approval.reason] ?? approval.reason} - requested{' '}
          {formatDate(approval.requested_at)}
        </div>
      </div>
      {approval.reason === 'indeterminate_side_effect' && <Badge tone="bad">Indeterminate</Badge>}
      <button
        type="button"
        className="btn"
        aria-label={`Review approval ${approval.id}`}
        onClick={onSelect}
      >
        Review
      </button>
    </li>
  );
}

function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

export function ApprovalDetail({ org, id }: { org: string; id: string }) {
  const query = useApproval(org, id);
  return (
    <QueryGate query={query} noun="approval">
      {(a) => <DetailBody org={org} approval={a} />}
    </QueryGate>
  );
}

function DetailBody({ org, approval }: { org: string; approval: Approval }) {
  const session = useSession();
  const indeterminate = approval.reason === 'indeterminate_side_effect';
  // The run tells us who initiated it when the approval payload does not. For an unknown write it
  // also tells us whether a retry is still possible (status and deadline), so it is always loaded.
  const run = useRun(
    org,
    approval.run_initiated_by && !indeterminate ? undefined : approval.run_id,
  );
  const now = useNow();
  const seconds = secondsUntil(approval.expires_at, now);
  // Retry needs a run that is known to be waiting within its deadline; if the run could not be
  // loaded it is withheld and the server's 422 would be shown for any other attempt.
  const retryAllowed = run.data ? canRetryUnknownWrite(run.data, now) : false;
  const initiator = isInitiator(approval, run.data, session.data?.user.id);
  const decidable =
    approval.status === 'pending' &&
    // No tenant-level check: it ignores workspace/project grants. The server decides, and a 403
    // from the decide call is shown by the form.
    !initiator &&
    // expires_at is informational for an unknown write: deciding after it is allowed (R1.1).
    (indeterminate || (seconds ?? 1) > 0);

  return (
    <section aria-label="Approval detail" className="card space-y-4">
      <h2 className="text-base font-semibold">
        {approval.tool?.key ?? REASON_LABEL[approval.reason] ?? 'Approval'}
      </h2>
      {indeterminate && (
        <div
          role="alert"
          className="rounded-md border-2 border-red-600 bg-red-50 p-3 text-sm text-red-900"
        >
          <p className="font-semibold">Indeterminate side effect</p>
          <p>
            This tool started but its outcome was never recorded. The side effect may or may not
            have happened. Check the target system before deciding. Retrying a non-idempotent action
            can duplicate it.
          </p>
        </div>
      )}
      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[10rem_1fr]">
        <dt className="text-slate-600">Reason</dt>
        <dd>{REASON_LABEL[approval.reason] ?? approval.reason}</dd>
        <dt className="text-slate-600">Tool</dt>
        <dd>
          {approval.tool ? (
            <>
              <code className="font-mono text-xs">{approval.tool.key}</code>
              {approval.tool.version !== undefined && <> version {approval.tool.version}</>}
            </>
          ) : (
            '-'
          )}
        </dd>
        <dt className="text-slate-600">Side-effect class</dt>
        <dd>{approval.tool?.side_effect_class ?? '-'}</dd>
        <dt className="text-slate-600">Requested by</dt>
        <dd>{principalLabel(approval.requested_by)}</dd>
        <dt className="text-slate-600">Requested</dt>
        <dd>{formatDate(approval.requested_at)}</dd>
        {!indeterminate && (
          <>
            <dt className="text-slate-600">Expires</dt>
            <dd>
              {formatDate(approval.expires_at)}{' '}
              <span role="timer" aria-live="off" className="font-medium">
                ({countdownText(seconds)})
              </span>
            </dd>
          </>
        )}
        <dt className="text-slate-600">Status</dt>
        <dd>{approval.status}</dd>
        <dt className="text-slate-600">Run</dt>
        <dd>
          <Link
            className="text-blue-800 underline"
            href={`${orgHref(org)}/runs/${approval.run_id}`}
          >
            {approval.run_id}
          </Link>
        </dd>
      </dl>
      <div>
        <h3 className="text-sm font-semibold">Exact arguments</h3>
        <p className="text-xs text-slate-600">
          The validated arguments the tool will receive (secrets redacted). Approve only what is
          shown here.
        </p>
        <pre
          aria-label="Exact arguments"
          tabIndex={0}
          className="mt-1 max-h-80 overflow-auto rounded bg-slate-100 p-2 font-mono text-xs"
        >
          {JSON.stringify(approval.arguments ?? null, null, 2)}
        </pre>
      </div>
      {approval.decision && (
        <p className="text-sm">
          Decision: <strong>{approval.decision.decision}</strong>
          {approval.decision.comment ? ` - ${approval.decision.comment}` : ''}
        </p>
      )}
      {approval.status === 'pending' && initiator && (
        <p role="note" className="text-sm text-slate-700">
          You started this run, so someone else must decide this approval.
        </p>
      )}
      {decidable && (
        <DecisionForm
          org={org}
          approval={approval}
          indeterminate={indeterminate}
          retryAllowed={retryAllowed}
        />
      )}
    </section>
  );
}

const INDETERMINATE: { decision: ApprovalDecision; label: string; help: string }[] = [
  {
    decision: 'mark_succeeded',
    label: 'Mark succeeded',
    help: 'The effect happened. Continue the run as if the tool succeeded.',
  },
  {
    decision: 'retry',
    label: 'Retry',
    help: 'The effect did not happen, or repeating it is safe. Run the tool again.',
  },
  {
    decision: 'fail',
    label: 'Fail',
    help: 'End the run as failed.',
  },
];

function DecisionForm({
  org,
  approval,
  indeterminate,
  retryAllowed,
}: {
  org: string;
  approval: Approval;
  indeterminate: boolean;
  retryAllowed: boolean;
}) {
  const qc = useQueryClient();
  const [comment, setComment] = useState('');
  const [confirming, setConfirming] = useState<ApprovalDecision | null>(null);
  const { busy, error, run } = useAction();
  const separation = error instanceof ApiError && error.code === 'separation_of_duties';

  async function decide(decision: ApprovalDecision) {
    const r = await run(() => endpoints.decideApproval(org, approval.id, decision, comment.trim()));
    if (r.ok) {
      setConfirming(null);
      await qc.invalidateQueries({ queryKey: ['operate', org] });
    }
  }

  const commentRequired = indeterminate && !comment.trim();
  return (
    <div className="space-y-3 border-t border-slate-200 pt-3">
      <TextField
        label={indeterminate ? 'Comment (required)' : 'Comment (optional)'}
        value={comment}
        onChange={setComment}
        multiline
        required={indeterminate}
      />
      {separation ? (
        <p role="alert" className="text-sm text-red-800">
          The server refused this decision: you started this run and self-approval is not allowed
          (separation of duties).
        </p>
      ) : (
        !confirming && <ActionError error={error} title="Could not record the decision" />
      )}
      {indeterminate && !retryAllowed && (
        <p role="note" className="text-xs text-slate-600">
          Retry is not offered: the run has ended or is past its deadline, or its state could not be
          loaded. Record whether the write happened (mark succeeded) or declare it failed (fail).
        </p>
      )}
      {indeterminate ? (
        <ul className="space-y-2">
          {INDETERMINATE.filter((d) => d.decision !== 'retry' || retryAllowed).map((d) => (
            <li key={d.decision} className="flex items-center gap-3">
              <button
                type="button"
                className={`btn ${d.decision === 'fail' ? 'btn-danger' : ''}`}
                disabled={busy || commentRequired}
                onClick={() => setConfirming(d.decision)}
              >
                {d.label}
              </button>
              <span className="text-xs text-slate-600">{d.help}</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="flex gap-2">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => void decide('approve')}
          >
            Approve
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy}
            onClick={() => void decide('reject')}
          >
            Reject
          </button>
        </div>
      )}
      <ConfirmDialog
        open={confirming !== null}
        title="Confirm decision on an indeterminate side effect"
        confirmLabel={`Confirm ${INDETERMINATE.find((d) => d.decision === confirming)?.label ?? ''}`}
        busy={busy}
        onConfirm={() => confirming && void decide(confirming)}
        onCancel={() => setConfirming(null)}
      >
        <p>
          You are about to record{' '}
          <strong>{INDETERMINATE.find((d) => d.decision === confirming)?.label}</strong> for{' '}
          <code>{approval.tool?.key ?? approval.id}</code>. This is audited and cannot be changed.
        </p>
        {separation ? (
          <p role="alert" className="mt-2 text-red-800">
            The server refused this decision (separation of duties).
          </p>
        ) : (
          <ActionError error={error} title="Could not record the decision" />
        )}
      </ConfirmDialog>
    </div>
  );
}
