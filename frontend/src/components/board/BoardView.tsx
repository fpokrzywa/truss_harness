'use client';

import Link from 'next/link';
import type {
  BoardOpenPlan,
  BoardProject,
  BoardRelease,
  BoardSolution,
  BoardTarget,
} from '@/lib/api/board-types';
import { useBoard } from '@/lib/board-hooks';
import { deliverHref, evaluationsHref, releaseHref } from '@/lib/scope';
import { Badge, EmptyState, formatDate, PageHeader, QueryGate } from '../admin/ui';
import { DeploymentStatusBadge } from '../deliver/DeploymentStatusBadge';
import { StatusBadge } from '../integrate/Maturity';
import { ProjectScopePicker, useProjectQuery } from '../releases/ProjectScope';
import { useOrgParam } from '../security/useOrg';

const enc = encodeURIComponent;
const linkClass = 'text-blue-800 underline';

/** Read-only release board: one grid per project. Server text renders as plain React text only. */
export function BoardView() {
  const org = useOrgParam();
  const { ws, proj } = useProjectQuery();
  const q = useBoard(org, { workspace: ws, project: proj });

  return (
    <div className="space-y-6">
      <PageHeader
        title="Board"
        description="What is deployed where: per solution and target, the current verified release, the last deployment attempt and any open plan."
      />
      <ProjectScopePicker org={org} />
      <QueryGate query={q} noun="board">
        {(data) => {
          const projects = data.pages.flatMap((p) => p.projects);
          return (
            <>
              {projects.length === 0 ? (
                <div className="card p-0">
                  <EmptyState>Nothing to show for this selection.</EmptyState>
                </div>
              ) : (
                projects.map((p) => (
                  <ProjectSection key={`${p.workspace.id}/${p.project.id}`} org={org} project={p} />
                ))
              )}
              {q.hasNextPage && (
                <button
                  type="button"
                  className="btn"
                  disabled={q.isFetchingNextPage}
                  onClick={() => void q.fetchNextPage()}
                >
                  {q.isFetchingNextPage ? 'Loading...' : 'Load more'}
                </button>
              )}
            </>
          );
        }}
      </QueryGate>
    </div>
  );
}

interface Column {
  target_id: string;
  name: string;
  environment: string;
  platform_kind: string;
}

/** The union of the solutions' targets, ordered by environment, then name. */
function boardColumns(solutions: readonly BoardSolution[]): Column[] {
  const byId = new Map<string, Column>();
  for (const s of solutions)
    for (const t of s.targets)
      if (!byId.has(t.target_id))
        byId.set(t.target_id, {
          target_id: t.target_id,
          name: t.name,
          environment: t.environment,
          platform_kind: t.platform_kind,
        });
  return [...byId.values()].sort(
    (a, b) => a.environment.localeCompare(b.environment) || a.name.localeCompare(b.name),
  );
}

/** Adjacent columns of one environment, for the header row's `colSpan`. */
function environmentGroups(columns: readonly Column[]): { environment: string; span: number }[] {
  const groups: { environment: string; span: number }[] = [];
  for (const c of columns) {
    const last = groups.at(-1);
    if (last && last.environment === c.environment) last.span += 1;
    else groups.push({ environment: c.environment, span: 1 });
  }
  return groups;
}

function ProjectSection({ org, project }: { org: string; project: BoardProject }) {
  const title = `${project.workspace.name} / ${project.project.name}`;
  const columns = boardColumns(project.solutions);
  return (
    <section aria-label={title} className="card overflow-x-auto p-0">
      <h2 className="p-4 pb-2 text-lg font-semibold text-slate-900">{title}</h2>
      {project.truncated && (
        <p className="px-4 pb-2 text-sm text-amber-900">
          Only part of this project&apos;s solutions is shown.
        </p>
      )}
      {project.solutions.length === 0 ? (
        <EmptyState>No solutions in this project.</EmptyState>
      ) : (
        <table className="w-full">
          <caption className="sr-only">{`Board ${title}`}</caption>
          <thead className="border-b border-slate-200">
            <tr>
              <td className="th" />
              {environmentGroups(columns).map((g, i) => (
                <th key={i} scope="colgroup" colSpan={g.span} className="th">
                  {g.environment}
                </th>
              ))}
            </tr>
            <tr>
              <th scope="col" className="th">
                Solution
              </th>
              {columns.map((c) => (
                <th key={c.target_id} scope="col" className="th">
                  <span className="block">{c.name}</span>
                  <span className="block font-mono text-xs font-normal">{c.platform_kind}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {project.solutions.map((s) => {
              const targets = new Map(s.targets.map((t) => [t.target_id, t]));
              return (
                <tr key={s.id}>
                  <th scope="row" className="td text-left align-top">
                    <SolutionHeader org={org} solution={s} />
                  </th>
                  {columns.map((c) => (
                    <td key={c.target_id} className="td align-top">
                      <Cell org={org} target={targets.get(c.target_id)} />
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function SolutionHeader({ org, solution }: { org: string; solution: BoardSolution }) {
  return (
    <div className="space-y-1">
      <div className="font-medium">
        {solution.name}
        {solution.status === 'archived' && (
          <>
            {' '}
            <Badge>archived</Badge>
          </>
        )}
      </div>
      {solution.releases.length > 0 && (
        <ul aria-label={`Releases of ${solution.name}`} className="space-y-1 text-sm font-normal">
          {solution.releases.map((r) => (
            <ReleaseItem key={r.id} org={org} release={r} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ReleaseItem({ org, release }: { org: string; release: BoardRelease }) {
  return (
    <li className="flex flex-wrap items-center gap-2">
      <Link className={linkClass} href={releaseHref(org, release.id)}>
        v{release.version_no}
      </Link>
      <code className="font-mono text-xs">{release.digest12}</code>
      {release.status === 'withdrawn' && <Badge tone="warn">withdrawn</Badge>}
      {release.evaluation && (
        <Link href={evaluationsHref(org, enc(release.evaluation.run_id))}>
          <span className="sr-only">Evaluation </span>
          <StatusBadge status={release.evaluation.status} />
        </Link>
      )}
    </li>
  );
}

/** Only `current` names the current version: an unknown outcome never supplies one. */
function Cell({ org, target }: { org: string; target: BoardTarget | undefined }) {
  if (!target || (!target.current && !target.last_attempt && !target.open_plan))
    return <span className="text-sm text-slate-500">No deployment</span>;
  const { current, last_attempt: attempt, open_plan: plan } = target;
  return (
    <div className="space-y-1 text-sm">
      {current && (
        <div>
          <Link className={`${linkClass} font-medium`} href={releaseHref(org, current.release_id)}>
            v{current.version_no}
          </Link>{' '}
          <span className="text-slate-600">verified {formatDate(current.verified_at)}</span>
        </div>
      )}
      {attempt && (
        <div>
          <Link href={deliverHref(org, `deployments/${enc(attempt.deployment_id)}`)}>
            <span className="sr-only">Last deployment </span>
            <DeploymentStatusBadge status={attempt.status} />
          </Link>
          {attempt.status_reason !== null && (
            <span className="block text-xs text-slate-600">{attempt.status_reason}</span>
          )}
        </div>
      )}
      {plan && <PlanLink org={org} plan={plan} />}
    </div>
  );
}

function PlanLink({ org, plan }: { org: string; plan: BoardOpenPlan }) {
  return (
    <div>
      <Link href={deliverHref(org, `plans/${enc(plan.plan_id)}`)}>
        <span className="sr-only">Plan </span>
        <Badge tone={plan.status === 'expired' ? 'bad' : 'neutral'}>{plan.status}</Badge>
        {plan.expires_at !== null && (
          <span className="ml-1 text-xs text-slate-600">expires {formatDate(plan.expires_at)}</span>
        )}
      </Link>
    </div>
  );
}
