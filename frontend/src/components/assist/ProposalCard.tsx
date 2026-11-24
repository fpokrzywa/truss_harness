'use client';

import Link from 'next/link';
import type { ProposalKind, ProposalOut } from '@/lib/api/assist-types';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import { PROPOSAL_KIND, PROPOSAL_STATUS } from '@/lib/assist';
import { useStoreProposal } from '@/lib/assist-hooks';
import { useTargets } from '@/lib/delivery-hooks';
import {
  appRequestHref,
  connectionHref,
  deliverHref,
  evaluationsHref,
  releaseHref,
} from '@/lib/scope';
import { ActionError, Badge, useAction } from '../admin/ui';
import { targetName } from '../deliver/targetName';
import { Field, linkClass } from './parts';

const notOpen = (e: unknown): boolean => e instanceof ApiError && e.code === 'proposal_not_open';

const RESULT_LABEL: Record<ProposalKind, string> = {
  appgen_request: 'Open the app request',
  evaluation_run: 'Open the evaluation run',
  delivery_plan: 'Open the plan',
};

/** One proposal: what it would create, and Accept and Dismiss for a user who may decide. */
export function ProposalCard({
  org,
  requestId,
  proposal,
  position,
  project,
  canDecide,
  onStale,
}: {
  org: string;
  requestId: string;
  proposal: ProposalOut;
  /** Its place in the list, which tells two proposals of one kind apart. */
  position: number;
  project: { ws: string; proj: string } | null;
  canDecide: boolean;
  /** The server says the proposal is no longer what is shown. */
  onStale: () => void;
}) {
  const store = useStoreProposal(org, requestId);
  const action = useAction();
  const { payload, status } = proposal;
  const kind = PROPOSAL_KIND[proposal.kind];
  const statusLabel = PROPOSAL_STATUS[status];
  const canAccept = canDecide && status === 'open' && payload !== null;
  const canDismiss = canDecide && (status === 'open' || status === 'failed');

  async function decide(call: typeof endpoints.acceptProposal) {
    await action.run(async () => {
      try {
        await store(await call(org, proposal.id));
      } catch (e) {
        if (notOpen(e)) onStale();
        throw e;
      }
    });
  }

  return (
    <section aria-label={`Proposal ${position}: ${kind}`} className="card space-y-3 text-sm">
      <h3 className="flex flex-wrap items-center gap-2 font-semibold">
        {kind}
        <span role="status">
          <Badge tone={statusLabel.tone}>{statusLabel.label}</Badge>
        </span>
      </h3>
      {payload === null ? (
        <p className="text-slate-700">You do not have access to this proposal&apos;s contents.</p>
      ) : (
        <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-[12rem_1fr]">
          <PayloadFields org={org} kind={proposal.kind} payload={payload} />
        </dl>
      )}
      {status === 'failed' && (
        <p className="text-red-900">
          Nothing was created: the request was refused
          {proposal.status_reason !== null && (
            <>
              {' '}
              with <code className="font-mono text-xs">{proposal.status_reason}</code>
            </>
          )}
          . A failed proposal can only be dismissed.
        </p>
      )}
      {status === 'accepted' && <Result org={org} proposal={proposal} project={project} />}
      {notOpen(action.error) ? (
        <p
          role="alert"
          className="rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900"
        >
          This proposal is no longer open: it was already decided, or someone else is accepting it
          now.
        </p>
      ) : (
        <ActionError error={action.error} title="Could not decide the proposal" />
      )}
      {(canAccept || canDismiss) && (
        <div className="flex flex-wrap gap-2">
          {canAccept && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={action.busy}
              onClick={() => void decide(endpoints.acceptProposal)}
            >
              Accept <span className="sr-only">proposal {position}</span>
            </button>
          )}
          {canDismiss && (
            <button
              type="button"
              className="btn"
              disabled={action.busy}
              onClick={() => void decide(endpoints.dismissProposal)}
            >
              Dismiss <span className="sr-only">proposal {position}</span>
            </button>
          )}
        </div>
      )}
    </section>
  );
}

/** A payload value as plain text; `-` when absent. */
const text = (v: unknown): string => (typeof v === 'string' && v !== '' ? v : '-');

/** An id that links to its object's screen. */
function IdLink({ value, href }: { value: unknown; href: (id: string) => string }) {
  return typeof value === 'string' && value !== '' ? (
    <Link className={`${linkClass} font-mono text-xs break-all`} href={href(value)}>
      {value}
    </Link>
  ) : (
    <>-</>
  );
}

function PayloadFields({
  org,
  kind,
  payload,
}: {
  org: string;
  kind: ProposalKind;
  payload: Record<string, unknown>;
}) {
  const release = (
    <Field label="Release">
      <IdLink value={payload.release_id} href={(id) => releaseHref(org, id)} />
    </Field>
  );
  switch (kind) {
    case 'appgen_request':
      return (
        <>
          <Field label="Brief">
            <span className="whitespace-pre-wrap">{text(payload.brief)}</span>
          </Field>
          <Field label="Tables">
            <ul className="font-mono text-xs">
              {(Array.isArray(payload.tables) ? payload.tables : []).map((t, i) => (
                <li key={i}>{text(t)}</li>
              ))}
            </ul>
          </Field>
          <Field label="Connection">
            <IdLink value={payload.connection_id} href={(id) => connectionHref(org, id)} />
          </Field>
          <Field label="Model id">
            <span className="font-mono text-xs break-all">{text(payload.model_id)}</span>
          </Field>
        </>
      );
    case 'evaluation_run':
      return release;
    case 'delivery_plan':
      return (
        <>
          <Field label="Application key">
            <span className="font-mono text-xs">{text(payload.application_key)}</span>
          </Field>
          <Field label="Display name">{text(payload.display_name)}</Field>
          {release}
          <Field label="Target">
            <TargetName org={org} id={text(payload.target_id)} />
          </Field>
        </>
      );
  }
}

function TargetName({ org, id }: { org: string; id: string }) {
  const targets = useTargets(org);
  return <>{targetName(targets.data?.items, id)}</>;
}

/** The object an accepted proposal created. */
function Result({
  org,
  proposal,
  project,
}: {
  org: string;
  proposal: ProposalOut;
  project: { ws: string; proj: string } | null;
}) {
  const ref = proposal.result_ref;
  if (ref === null)
    return <p className="text-slate-700">You do not have access to what this proposal created.</p>;
  const id = encodeURIComponent(ref.id);
  const href = {
    appgen_request: appRequestHref(org, ref.id, project?.ws, project?.proj),
    evaluation_run: evaluationsHref(org, id),
    delivery_plan: deliverHref(org, `plans/${id}`),
  }[ref.kind];
  return (
    <p>
      <Link className={linkClass} href={href}>
        {RESULT_LABEL[ref.kind]}
      </Link>
    </p>
  );
}
