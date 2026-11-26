'use client';

import Link from 'next/link';
import type { PlanOut } from '@/lib/api/delivery-types';
import { usePlan, useTargets } from '@/lib/delivery-hooks';
import { countdownText, secondsUntil } from '@/lib/run-utils';
import { deliverHref } from '@/lib/scope';
import { useNow } from '@/lib/use-now';
import { EmptyState, formatDate, PageHeader, QueryGate } from '../admin/ui';
import { StatusBadge } from '../integrate/Maturity';
import { useOrgParam } from '../security/useOrg';
import { planNote } from './platformKinds';
import { planFailureHint } from './refusalHints';
import { targetName } from './targetName';

/** One deployment plan: its sealed digest, steps and decision. Server text is plain React text. */
export function PlanDetailView({ id }: { id: string }) {
  const org = useOrgParam();
  const plan = usePlan(org, id);
  return (
    <div className="space-y-6">
      <QueryGate query={plan} noun="plan">
        {(p) => <PlanBody org={org} plan={p} />}
      </QueryGate>
    </div>
  );
}

function PlanBody({ org, plan }: { org: string; plan: PlanOut }) {
  const targets = useTargets(org);
  const target = targets.data?.items.find((t) => t.id === plan.target_id);
  const note = target ? planNote(target.platform_kind) : undefined;
  const failureHint =
    plan.status === 'plan_failed' ? planFailureHint(plan.status_reason) : undefined;
  return (
    <>
      <PageHeader title={plan.display_name} description="Deployment plan" />
      <section aria-label="Plan" className="card space-y-4">
        {note && <p className="text-sm">{note}</p>}
        <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
          <dt className="text-slate-600">Status</dt>
          <dd>
            <StatusBadge status={plan.status} />
            {plan.status_reason && <span className="ml-2">{plan.status_reason}</span>}
            {failureHint && <p className="mt-1">{failureHint}</p>}
          </dd>
          {plan.status === 'sealed' && (
            <>
              <dt className="text-slate-600">Expires</dt>
              <dd>
                {formatDate(plan.expires_at)} <Countdown expiresAt={plan.expires_at} />
              </dd>
            </>
          )}
          <dt className="text-slate-600">Target</dt>
          <dd>{targetName(targets.data?.items, plan.target_id)}</dd>
          <dt className="text-slate-600">Application key</dt>
          <dd className="font-mono text-xs">{plan.application_key}</dd>
          <dt className="text-slate-600">Display name</dt>
          <dd>{plan.display_name}</dd>
          <dt className="text-slate-600">Plan digest</dt>
          <dd className="font-mono text-xs break-all">{plan.digest ?? 'Not sealed yet'}</dd>
          <dt className="text-slate-600">Artifact digest</dt>
          <dd className="font-mono text-xs break-all">{plan.artifact_digest}</dd>
          <dt className="text-slate-600">Adapter version</dt>
          <dd>{plan.adapter_version ?? '-'}</dd>
          <dt className="text-slate-600">Toolchain digest</dt>
          <dd className="font-mono text-xs break-all">{plan.toolchain_digest ?? '-'}</dd>
          <dt className="text-slate-600">Created</dt>
          <dd>{formatDate(plan.created_at)}</dd>
          {plan.deployment_id && (
            <>
              <dt className="text-slate-600">Deployment</dt>
              <dd>
                <Link
                  className="text-blue-800 underline"
                  href={deliverHref(org, `deployments/${encodeURIComponent(plan.deployment_id)}`)}
                >
                  {plan.deployment_id}
                </Link>
              </dd>
            </>
          )}
        </dl>
      </section>

      <section aria-label="Steps" className="card overflow-x-auto p-0">
        <h2 className="px-4 pt-4 text-base font-semibold">Steps</h2>
        {!plan.steps || plan.steps.length === 0 ? (
          <EmptyState>No steps planned yet.</EmptyState>
        ) : (
          <table className="w-full">
            <caption className="sr-only">Plan steps</caption>
            <thead className="border-b border-slate-200">
              <tr>
                {['Key', 'Action', 'Resource', 'Reversible'].map((h) => (
                  <th key={h} scope="col" className="th">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {plan.steps.map((s) => (
                <tr key={s.key}>
                  <td className="td font-mono text-xs">{s.key}</td>
                  <td className="td">{s.action}</td>
                  <td className="td font-mono text-xs">{s.resource_key}</td>
                  <td className="td">{s.reversible ? 'Yes' : 'No'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {plan.approval && (
        <section aria-label="Decision" className="card">
          <h2 className="text-base font-semibold">Decision</h2>
          <dl className="mt-2 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
            <dt className="text-slate-600">Decision</dt>
            <dd>{plan.approval.decision === 'approve' ? 'Approved' : 'Rejected'}</dd>
            <dt className="text-slate-600">Decided</dt>
            <dd>{formatDate(plan.approval.decided_at)}</dd>
            <dt className="text-slate-600">Note</dt>
            <dd>{plan.approval.note ?? '-'}</dd>
          </dl>
        </section>
      )}
    </>
  );
}

/** Live time left until `expiresAt`, ticking once a second. */
export function Countdown({ expiresAt }: { expiresAt: string | null }) {
  const now = useNow();
  return (
    <span role="timer" aria-live="off" className="font-medium">
      ({countdownText(secondsUntil(expiresAt, now))})
    </span>
  );
}
