'use client';

import Link from 'next/link';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { DeploymentDetail, VerificationCheck } from '@/lib/api/delivery-types';
import { useDeployment, usePlan, useRefreshDelivery, useTargets } from '@/lib/delivery-hooks';
import { useCan } from '@/lib/hooks';
import { platformLabel } from '@/lib/platforms';
import { useRelease } from '@/lib/release-hooks';
import { deliverHref } from '@/lib/scope';
import { ConfirmDialog } from '../ConfirmDialog';
import {
  ActionError,
  Badge,
  EmptyState,
  formatDate,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { AskSethButton } from '../assist/AskSethButton';
import { MaturityBadge } from '../integrate/Maturity';
import { useOrgParam } from '../security/useOrg';
import { DeploymentStatusBadge } from './DeploymentStatusBadge';
import { isUnusable } from './platformKinds';
import { refusalHint } from './refusalHints';
import { targetName } from './targetName';

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

/** The executor marks fake-platform receipts: top level of journal receipts, handles of the apply. */
function onFakePlatform(d: DeploymentDetail): boolean {
  const handles = d.receipt?.operation_handles;
  return (
    d.receipt?.platform === 'fake' ||
    (isRecord(handles) && handles.platform === 'fake') ||
    d.journal.some((s) => s.receipt?.platform === 'fake')
  );
}

function verificationChecks(d: DeploymentDetail): VerificationCheck[] | null {
  const checks = d.verification?.checks;
  return Array.isArray(checks) ? (checks.filter(isRecord) as unknown as VerificationCheck[]) : null;
}

const json = (v: unknown) => JSON.stringify(v, null, 2);

/** The reason of a deployment whose apply found the target changed since the plan. */
const PLAN_STALE_REASON = 'apply refused: plan_stale';

/** One deployment: status, journal, receipt and verification. Server text is plain React text. */
export function DeploymentDetailView({ id }: { id: string }) {
  const org = useOrgParam();
  const deployment = useDeployment(org, id);
  return (
    <div className="space-y-6">
      <QueryGate query={deployment} noun="deployment">
        {(d) => <DeploymentBody org={org} deployment={d} />}
      </QueryGate>
    </div>
  );
}

function DeploymentBody({ org, deployment: d }: { org: string; deployment: DeploymentDetail }) {
  const { can } = useCan(org);
  const targets = useTargets(org);
  const refresh = useRefreshDelivery(org);
  const cancel = useAction();
  const reconcile = useAction();
  const [cancelling, setCancelling] = useState(false);
  const [markingFailed, setMarkingFailed] = useState(false);
  const canResolve = can('delivery:deployment:resolve');
  const checks = verificationChecks(d);
  const target = targets.data?.items.find((t) => t.id === d.target_id);
  const unusable = target !== undefined && isUnusable(target.bindings);

  async function confirmCancel() {
    const res = await cancel.run(() => endpoints.cancelDeployment(org, d.id));
    setCancelling(false);
    if (res.ok) await refresh('deployments', 'plans');
  }

  async function runReconcile() {
    const res = await reconcile.run(() => endpoints.reconcileDeployment(org, d.id));
    if (res.ok) await refresh('deployments');
  }

  return (
    <>
      <PageHeader
        title={d.application_key}
        description="Deployment"
        actions={
          <>
            <AskAboutDeployment org={org} deployment={d} />
            {canResolve && d.status === 'queued' && (
              <button type="button" className="btn" onClick={() => setCancelling(true)}>
                Cancel deployment
              </button>
            )}
          </>
        }
      />
      <ActionError error={cancel.error} title="Could not cancel the deployment" />

      {d.status === 'unknown_outcome' && (
        <div
          role="alert"
          className="space-y-2 rounded-md border-2 border-red-600 bg-red-50 p-3 text-sm text-red-900"
        >
          <p className="font-semibold">Outcome unknown</p>
          <p>
            The platform write may or may not have taken effect. An operator must reconcile this
            deployment against the platform or mark it failed.
          </p>
          {unusable && (
            <p>
              This target&apos;s stored settings are no longer valid, so this deployment cannot be
              reconciled. Check the platform, then mark the deployment as failed.
            </p>
          )}
          {canResolve && (
            <div className="flex gap-2">
              {!unusable && (
                <button
                  type="button"
                  className="btn"
                  disabled={reconcile.busy}
                  onClick={() => void runReconcile()}
                >
                  {reconcile.busy ? 'Working...' : 'Reconcile'}
                </button>
              )}
              <button
                type="button"
                className="btn btn-danger"
                onClick={() => setMarkingFailed(true)}
              >
                Mark as failed
              </button>
            </div>
          )}
          <ActionError error={reconcile.error} title="Could not reconcile the deployment" />
        </div>
      )}

      <section aria-label="Deployment" className="card">
        <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
          <dt className="text-slate-600">Status</dt>
          <dd className="flex flex-wrap items-center gap-2">
            <DeploymentStatusBadge status={d.status} />
            {onFakePlatform(d) && <Badge tone="warn">Fake platform</Badge>}
          </dd>
          <dt className="text-slate-600">Status reason</dt>
          <dd>
            {d.status_reason ?? '-'}
            {d.status_reason === PLAN_STALE_REASON && (
              <p className="mt-1">{refusalHint('plan_stale')}</p>
            )}
          </dd>
          <dt className="text-slate-600">Kind</dt>
          <dd className="flex flex-wrap items-center gap-2">
            {target ? (
              <>
                {platformLabel(target.platform_kind)} <MaturityBadge maturity="experimental" />
              </>
            ) : (
              '-'
            )}
          </dd>
          <dt className="text-slate-600">Application key</dt>
          <dd className="font-mono text-xs">{d.application_key}</dd>
          <dt className="text-slate-600">Target</dt>
          <dd>{targetName(targets.data?.items, d.target_id)}</dd>
          <dt className="text-slate-600">Plan</dt>
          <dd>
            <Link
              className="text-blue-800 underline"
              href={deliverHref(org, `plans/${encodeURIComponent(d.plan_id)}`)}
            >
              {d.plan_id}
            </Link>
          </dd>
          {d.observe_deadline && (
            <>
              <dt className="text-slate-600">Observe deadline</dt>
              <dd>{formatDate(d.observe_deadline)}</dd>
            </>
          )}
          <dt className="text-slate-600">Created</dt>
          <dd>{formatDate(d.created_at)}</dd>
          <dt className="text-slate-600">Updated</dt>
          <dd>{formatDate(d.updated_at)}</dd>
          {d.status === 'resolved_failed' && (
            <>
              <dt className="text-slate-600">Resolved</dt>
              <dd>{formatDate(d.resolved_at)}</dd>
              <dt className="text-slate-600">Resolution note</dt>
              <dd>{d.resolved_note ?? '-'}</dd>
            </>
          )}
        </dl>
      </section>

      <section aria-label="Journal" className="card overflow-x-auto p-0">
        <h2 className="px-4 pt-4 text-base font-semibold">Journal</h2>
        {d.journal.length === 0 ? (
          <EmptyState>No steps recorded yet.</EmptyState>
        ) : (
          <ol className="divide-y divide-slate-100">
            {d.journal.map((s, i) => (
              <li key={`${s.step_key}-${s.state}-${i}`} className="px-4 py-2 text-sm">
                <span className="font-mono text-xs">{s.step_key}</span>{' '}
                <Badge tone={s.state === 'complete' ? 'good' : 'neutral'}>{s.state}</Badge>{' '}
                <span className="text-slate-600">{formatDate(s.recorded_at)}</span>
                <p className="font-mono text-xs break-all text-slate-600">
                  Input digest {s.input_digest}
                </p>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section aria-label="Receipt" className="card">
        <h2 className="text-base font-semibold">Receipt</h2>
        {d.receipt ? (
          <pre
            aria-label="Receipt JSON"
            tabIndex={0}
            className="mt-2 max-h-80 overflow-auto rounded bg-slate-100 p-2 font-mono text-xs"
          >
            {json(d.receipt)}
          </pre>
        ) : (
          <p className="mt-2 text-sm text-slate-600">No receipt yet</p>
        )}
      </section>

      <section aria-label="Verification" className="card overflow-x-auto">
        <h2 className="text-base font-semibold">Verification</h2>
        {checks ? (
          <table className="mt-2 w-full">
            <caption className="sr-only">Verification checks</caption>
            <thead className="border-b border-slate-200">
              <tr>
                {['Check', 'Status', 'Required', 'Evidence'].map((h) => (
                  <th key={h} scope="col" className="th">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {checks.map((c, i) => (
                <tr key={`${String(c.name)}-${i}`}>
                  <td className="td">{String(c.name)}</td>
                  <td className="td">
                    <Badge
                      tone={
                        c.status === 'passed' ? 'good' : c.status === 'failed' ? 'bad' : 'neutral'
                      }
                    >
                      {c.status === 'not_run' ? 'Not run' : String(c.status)}
                    </Badge>
                  </td>
                  <td className="td">{c.required === false ? 'No' : 'Yes'}</td>
                  <td className="td break-all">{String(c.evidence ?? '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="mt-2 text-sm text-slate-600">Not verified yet</p>
        )}
      </section>

      {d.reconcile_evidence && (
        <section aria-label="Reconcile evidence" className="card">
          <h2 className="text-base font-semibold">Reconcile evidence</h2>
          <pre
            tabIndex={0}
            className="mt-2 max-h-80 overflow-auto rounded bg-slate-100 p-2 font-mono text-xs"
          >
            {json(d.reconcile_evidence)}
          </pre>
        </section>
      )}

      {markingFailed && (
        <MarkFailedDialog org={org} id={d.id} onClose={() => setMarkingFailed(false)} />
      )}
      <ConfirmDialog
        open={cancelling}
        title="Cancel deployment?"
        confirmLabel="Cancel deployment"
        busy={cancel.busy}
        onConfirm={() => void confirmCancel()}
        onCancel={() => setCancelling(false)}
      >
        <p>
          <strong>{d.application_key}</strong> is not deployed. A deployment can be cancelled only
          while it is queued.
        </p>
      </ConfirmDialog>
    </>
  );
}

/**
 * "Ask SETH" in the deployment's project: its target's, or, on a workspace-level target, the one of
 * the release it deploys.
 */
function AskAboutDeployment({ org, deployment: d }: { org: string; deployment: DeploymentDetail }) {
  const target = useTargets(org).data?.items.find((t) => t.id === d.target_id);
  if (!target?.workspace) return null;
  return target.project ? (
    <AskSethButton
      org={org}
      ws={target.workspace}
      proj={target.project}
      context={{ kind: 'deployment', id: d.id }}
    />
  ) : (
    <AskInPlanProject org={org} deployment={d} ws={target.workspace} />
  );
}

function AskInPlanProject({
  org,
  deployment: d,
  ws,
}: {
  org: string;
  deployment: DeploymentDetail;
  ws: string;
}) {
  const releaseId = usePlan(org, d.plan_id).data?.release_id;
  return releaseId ? (
    <AskInReleaseProject org={org} deployment={d} ws={ws} releaseId={releaseId} />
  ) : null;
}

function AskInReleaseProject({
  org,
  deployment: d,
  ws,
  releaseId,
}: {
  org: string;
  deployment: DeploymentDetail;
  ws: string;
  releaseId: string;
}) {
  const release = useRelease(org, releaseId).data;
  return release?.project && release.workspace === ws ? (
    <AskSethButton
      org={org}
      ws={ws}
      proj={release.project}
      context={{ kind: 'deployment', id: d.id }}
    />
  ) : null;
}

function MarkFailedDialog({ org, id, onClose }: { org: string; id: string; onClose: () => void }) {
  const refresh = useRefreshDelivery(org);
  const action = useAction();
  const [note, setNote] = useState('');
  const trimmed = note.trim();

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (trimmed === '' || trimmed.length > 2000) return;
    const res = await action.run(() => endpoints.markDeploymentFailed(org, id, trimmed));
    if (res.ok) {
      await refresh('deployments');
      onClose();
    }
  }

  return (
    <Modal title="Mark deployment as failed" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <p className="text-sm text-slate-700">
          Record what you found on the platform. The deployment ends as failed.
        </p>
        <TextField
          label="Note"
          value={note}
          onChange={setNote}
          multiline
          required
          maxLength={2000}
        />
        <ActionError error={action.error} title="Could not mark the deployment as failed" />
        <ModalActions
          onCancel={onClose}
          busy={action.busy}
          submitLabel="Mark as failed"
          submitDisabled={trimmed === '' || trimmed.length > 2000}
        />
      </form>
    </Modal>
  );
}
