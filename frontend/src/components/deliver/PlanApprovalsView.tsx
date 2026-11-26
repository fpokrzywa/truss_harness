'use client';

import Link from 'next/link';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import type { PlanOut } from '@/lib/api/delivery-types';
import { usePlans, useRefreshDelivery, useTargets } from '@/lib/delivery-hooks';
import { useCan, useSession } from '@/lib/hooks';
import { countdownText, secondsUntil } from '@/lib/run-utils';
import { deliverHref } from '@/lib/scope';
import { useNow } from '@/lib/use-now';
import {
  ActionError,
  EmptyState,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { refusalHint } from './refusalHints';
import { targetName } from './targetName';

const COLUMNS = ['Plan', 'Target', 'Digest', 'Requested by', 'Time left'];
const OWN_PLAN_REASON = 'You requested this plan; a different person must approve or reject it.';

type Deciding = { plan: PlanOut; decision: 'approve' | 'reject' };

/** Sealed plans waiting for a second person's decision. The server enforces the same rules. */
export function PlanApprovalsView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const session = useSession();
  const plans = usePlans(org, { status: 'sealed' });
  const targets = useTargets(org);
  const [deciding, setDeciding] = useState<Deciding | null>(null);
  const canApprove = can('delivery:plan:approve');

  return (
    <div className="space-y-6">
      <PageHeader
        title="Plan approvals"
        description="Sealed plans waiting for a decision. The person who requested a plan cannot approve or reject it."
      />
      <QueryGate query={plans} noun="plans">
        {(data) => (
          <div className="card overflow-x-auto p-0">
            {data.items.length === 0 ? (
              <EmptyState>No plans are waiting for a decision.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Plans waiting for a decision</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {COLUMNS.map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                    <th scope="col" className="th">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((p) => (
                    <PlanRow
                      key={p.id}
                      org={org}
                      plan={p}
                      target={targetName(targets.data?.items, p.target_id)}
                      own={p.requested_by === session.data?.user.id}
                      canApprove={canApprove}
                      onDecide={(decision) => setDeciding({ plan: p, decision })}
                    />
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </QueryGate>

      {deciding && <DecisionDialog org={org} {...deciding} onClose={() => setDeciding(null)} />}
    </div>
  );
}

function PlanRow({
  org,
  plan,
  target,
  own,
  canApprove,
  onDecide,
}: {
  org: string;
  plan: PlanOut;
  target: string;
  own: boolean;
  canApprove: boolean;
  onDecide: (decision: 'approve' | 'reject') => void;
}) {
  const now = useNow();
  const seconds = secondsUntil(plan.expires_at, now);
  const expired = seconds !== null && seconds <= 0;
  const disabled = own || expired;
  return (
    <tr>
      <td className="td font-medium">
        <Link
          className="text-blue-800 underline"
          href={deliverHref(org, `plans/${encodeURIComponent(plan.id)}`)}
        >
          {plan.display_name}
        </Link>
      </td>
      <td className="td">{target}</td>
      <td className="td font-mono text-xs break-all">{plan.digest ?? '-'}</td>
      <td className="td font-mono text-xs">{plan.requested_by}</td>
      <td className="td">
        <span role="timer" aria-live="off" className="font-medium">
          {countdownText(seconds)}
        </span>
      </td>
      <td className="td">
        {canApprove && (
          <div className="flex flex-col items-end gap-1">
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className="btn btn-primary"
                disabled={disabled}
                onClick={() => onDecide('approve')}
              >
                Approve <span className="sr-only">{plan.display_name}</span>
              </button>
              <button
                type="button"
                className="btn"
                disabled={disabled}
                onClick={() => onDecide('reject')}
              >
                Reject <span className="sr-only">{plan.display_name}</span>
              </button>
            </div>
            {own && <p className="text-xs text-slate-700">{OWN_PLAN_REASON}</p>}
          </div>
        )}
      </td>
    </tr>
  );
}

function DecisionDialog({
  org,
  plan,
  decision,
  onClose,
}: Deciding & { org: string; onClose: () => void }) {
  const refresh = useRefreshDelivery(org);
  const action = useAction();
  const [note, setNote] = useState('');
  const approve = decision === 'approve';
  const hint = action.error instanceof ApiError ? refusalHint(action.error.code) : undefined;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const trimmed = note.trim();
    const body = trimmed ? { note: trimmed } : {};
    const res = await action.run(() =>
      approve
        ? endpoints.approvePlan(org, plan.id, { plan_digest: plan.digest ?? '', ...body })
        : endpoints.rejectPlan(org, plan.id, body),
    );
    if (res.ok) {
      await refresh('plans', 'deployments');
      onClose();
    }
  }

  return (
    <Modal title={approve ? 'Approve plan?' : 'Reject plan?'} onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <p className="text-sm text-slate-700">
          <strong>{plan.display_name}</strong>
          {approve
            ? ' is deployed as sealed. You approve exactly this plan digest:'
            : ' is rejected and cannot be deployed.'}
        </p>
        {approve && (
          <p className="rounded bg-slate-100 p-2 font-mono text-xs break-all">{plan.digest}</p>
        )}
        <TextField
          label="Note (optional)"
          value={note}
          onChange={setNote}
          multiline
          maxLength={2000}
        />
        <ActionError
          error={action.error}
          title={approve ? 'Could not approve the plan' : 'Could not reject the plan'}
        />
        {hint && <p className="text-sm">{hint}</p>}
        <ModalActions
          onCancel={onClose}
          busy={action.busy}
          submitLabel={approve ? 'Approve plan' : 'Reject plan'}
        />
      </form>
    </Modal>
  );
}
