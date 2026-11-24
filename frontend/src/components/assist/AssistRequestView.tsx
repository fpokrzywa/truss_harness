'use client';

import Link from 'next/link';
import type { AssistRequestOut, RunRefOut } from '@/lib/api/assist-types';
import { ApiError } from '@/lib/api/errors';
import {
  CONTEXT_LABEL,
  contextHref,
  METHOD_LABEL,
  reasonText,
  REQUEST_STATUS,
  specialistTitle,
} from '@/lib/assist';
import {
  isAssistRouting,
  isAssistWorking,
  useAssistCatalog,
  useAssistRequest,
  useProjectSlugs,
} from '@/lib/assist-hooks';
import { failureInfo } from '@/lib/failure';
import { usePermissions } from '@/lib/hooks';
import { useRunResult } from '@/lib/operate-hooks';
import { assistHref, governHref, orgHref } from '@/lib/scope';
import { ActionError, EmptyState, formatDate, PageHeader, QueryGate } from '../admin/ui';
import { ErrorMessage } from '../ErrorMessage';
import { ResultView, RunStatusChip } from '../operate/parts';
import { useOrgParam } from '../security/useOrg';
import { Field, linkClass, RequestStatusBadge } from './parts';
import { ProposalCard } from './ProposalCard';

/** One request: its route, run, answer and proposals. Server text is plain React text or markdown. */
export function AssistRequestView({ id }: { id: string }) {
  const org = useOrgParam();
  const { query, polling, timedOut } = useAssistRequest(org, id);
  const gone = query.error instanceof ApiError && query.error.isNoAccess;
  // A failed refetch keeps the request on screen, with the error, unless access was lost.
  return (
    <div className="space-y-6">
      {query.data !== undefined && !gone ? (
        <>
          <ActionError error={query.error} title="Could not refresh the request" />
          <RequestBody
            org={org}
            request={query.data}
            polling={polling}
            timedOut={timedOut}
            refetch={() => void query.refetch()}
          />
        </>
      ) : (
        <QueryGate query={query} noun="request">
          {() => null}
        </QueryGate>
      )}
    </div>
  );
}

function progressText({ status, run }: AssistRequestOut): string {
  if (run === null) return `${REQUEST_STATUS[status].label}...`;
  return run.status === 'waiting_for_approval'
    ? 'The run is waiting for an approval...'
    : 'The specialist is working...';
}

function RequestBody({
  org,
  request,
  polling,
  timedOut,
  refetch,
}: {
  org: string;
  request: AssistRequestOut;
  polling: boolean;
  timedOut: boolean;
  refetch: () => void;
}) {
  const project = useProjectSlugs(org, request.workspace_id, request.project_id);
  // No project, no permission check: without slugs the query would answer for the organization.
  const permissions = usePermissions(project ? org : undefined, project?.ws, project?.proj);
  const can = (permission: string) => permissions.data?.has(permission) ?? false;
  const ended = request.status === 'refused' || request.status === 'failed';

  return (
    <>
      <PageHeader
        title="Ask SETH request"
        description={`Sent ${formatDate(request.created_at)}`}
        actions={
          ended &&
          project && (
            <Link
              className="btn btn-primary"
              href={assistHref(org, project.ws, project.proj, request.context)}
            >
              Ask again
            </Link>
          )
        }
      />
      {project && (
        <p className="text-sm">
          <Link className={linkClass} href={assistHref(org, project.ws, project.proj)}>
            Back to Ask SETH
          </Link>
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <p role="status" aria-live="polite" className="text-sm text-slate-700">
          {polling
            ? progressText(request)
            : timedOut
              ? 'Still in progress, but this page stopped checking after 10 minutes.'
              : null}
        </p>
        {timedOut && (
          <button type="button" className="btn" onClick={refetch}>
            Refresh
          </button>
        )}
      </div>
      <RouteSection org={org} request={request} project={project} />
      <RunSection org={org} request={request} canSeeCost={can('usage:read')} />
      {request.run?.status === 'completed' && <Answer org={org} runId={request.run.id} />}
      <section aria-label="Proposals" className="space-y-4">
        <h2 className="text-lg font-semibold text-slate-900">Proposals</h2>
        {request.proposals.length === 0 ? (
          <div className="card p-0">
            <EmptyState>
              {isAssistWorking(request) ? 'No proposals yet.' : 'No proposals.'}
            </EmptyState>
          </div>
        ) : (
          request.proposals.map((p, i) => (
            <ProposalCard
              key={p.id}
              org={org}
              requestId={request.id}
              proposal={p}
              position={i + 1}
              project={project}
              canDecide={can('assist:proposal:accept')}
              onStale={refetch}
            />
          ))
        )}
      </section>
    </>
  );
}

function RouteSection({
  org,
  request,
  project,
}: {
  org: string;
  request: AssistRequestOut;
  project: { ws: string; proj: string } | null;
}) {
  const catalog = useAssistCatalog(org);
  const title = (key: string | null) =>
    key === null ? '-' : specialistTitle(catalog.data?.entries, key);
  const { context, status_reason: reason } = request;
  const href = context && contextHref(org, context, project);
  const contextText = context && (
    <>
      {CONTEXT_LABEL[context.kind]}{' '}
      <span className="font-mono text-xs break-all">{context.id}</span>
    </>
  );
  return (
    <section aria-label="Request" className="card">
      <h2 className="mb-2 text-base font-semibold">Route</h2>
      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
        <Field label="Status">
          <RequestStatusBadge status={request.status} />
        </Field>
        {reason !== null && (
          <Field label="Reason">
            {reasonText(reason) !== reason && `${reasonText(reason)} `}
            <code className="font-mono text-xs">{reason}</code>
          </Field>
        )}
        <Field label="Specialist">{title(request.specialist_key)}</Field>
        <Field label="Route method">
          {request.route_method === null ? '-' : METHOD_LABEL[request.route_method]}
        </Field>
        <Field label="Rule">{request.rule_id ?? '-'}</Field>
        <Field label="Router version">{request.router_version ?? '-'}</Field>
        <Field label="Requested specialist">{title(request.requested_specialist)}</Field>
        <Field label="Candidates">{request.candidates.map(title).join(', ') || '-'}</Field>
        <Field label="About">
          {href ? (
            <Link className={linkClass} href={href}>
              {contextText}
            </Link>
          ) : (
            (contextText ?? '-')
          )}
        </Field>
      </dl>
    </section>
  );
}

function RunSection({
  org,
  request,
  canSeeCost,
}: {
  org: string;
  request: AssistRequestOut;
  canSeeCost: boolean;
}) {
  const { run } = request;
  return (
    <section aria-label="Run" className="card space-y-2 text-sm">
      <h2 className="text-base font-semibold">Run</h2>
      {run !== null ? (
        <RunDetails org={org} run={run} canSeeCost={canSeeCost} />
      ) : (
        <p className="text-slate-700">
          {request.run_id !== null
            ? 'The run cannot be shown: it is gone, or you do not have access to it.'
            : isAssistRouting(request)
              ? 'The run has not started yet: it starts once the request is routed.'
              : 'No run was started for this request.'}
        </p>
      )}
    </section>
  );
}

function RunDetails({
  org,
  run,
  canSeeCost,
}: {
  org: string;
  run: RunRefOut;
  canSeeCost: boolean;
}) {
  const failure = failureInfo(run.error_code);
  const id = encodeURIComponent(run.id);
  return (
    <>
      <p>
        <RunStatusChip status={run.status} />
      </p>
      {run.status === 'waiting_for_approval' && (
        <p>
          The run waits for an approval.{' '}
          <Link className={linkClass} href={`${orgHref(org)}/approvals`}>
            Open the approval queue
          </Link>
        </p>
      )}
      {failure && (
        <div className="rounded-md border border-red-300 bg-red-50 p-3 text-red-900">
          <p>
            <span className="font-semibold">{failure.title}</span>{' '}
            <code className="font-mono text-xs">{run.error_code}</code>
          </p>
          <p>{failure.explanation}</p>
        </div>
      )}
      <p className="flex flex-wrap gap-4">
        <Link className={linkClass} href={`${orgHref(org)}/runs/${id}`}>
          Open the run
        </Link>
        {canSeeCost && (
          <Link className={linkClass} href={governHref(org, `runs/${id}/cost`)}>
            Cost of the run
          </Link>
        )}
      </p>
    </>
  );
}

function Answer({ org, runId }: { org: string; runId: string }) {
  const result = useRunResult(org, runId, true);
  return (
    <section aria-label="Answer" className="space-y-2">
      <h2 className="text-lg font-semibold text-slate-900">Answer</h2>
      {result.isPending ? (
        <p role="status" className="text-sm text-slate-600">
          Loading the answer...
        </p>
      ) : result.isError ? (
        <ErrorMessage error={result.error} title="Could not load the answer" />
      ) : (
        <ResultView result={result.data} />
      )}
    </section>
  );
}
