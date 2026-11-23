/** Command center view model: turns reported platform state into what the screen shows. */

import type { HealthDetails } from './api/governance-types';
import type { Run } from './api/operate-types';
import type { PlatformInstallation, PlatformStatus, Readiness } from './api/types';
import { visibleNav, type ResolvedNavItem } from './nav';
import { STATUS_LABEL, UNKNOWN_OUTCOME_LABEL } from './run-utils';
import { parseScope } from './scope';

/** good: healthy. active: work in flight. warn: needs a person. idle: not in use, nothing wrong. */
export type Tone = 'good' | 'active' | 'warn' | 'idle';

export interface SystemNode {
  id: string;
  name: string;
  role: string;
  tone: Tone;
  /** Plain-language state. */
  state: string;
  /** The value the server reported, shown unchanged. */
  raw: string;
  detail?: string;
  lastSeenAt?: string | null;
}

export interface SystemInput {
  status: PlatformStatus;
  ready: { status: number; body: Readiness | undefined };
  /** Platform-admin endpoints: absent for everyone else. */
  details?: HealthDetails;
  installation?: PlatformInstallation;
}

const GOOD = new Set(['ok', 'ready', 'healthy', 'alive', 'running']);
const IDLE = new Set(['not_deployed']);

/** Fails safe: a value this screen does not recognise asks for attention rather than reading as fine. */
export function statusTone(raw: string): Tone {
  if (GOOD.has(raw)) return 'good';
  if (IDLE.has(raw)) return 'idle';
  return 'warn';
}

export function humanize(raw: string): string {
  if (raw === 'ok') return 'OK';
  const s = raw.replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function fromRaw(
  raw: string,
  node: Pick<SystemNode, 'id' | 'name' | 'role' | 'detail' | 'lastSeenAt'>,
): SystemNode {
  return { ...node, raw, tone: statusTone(raw), state: humanize(raw) };
}

/** One entry per component: these are the truss joints. */
export function buildNodes({ status, ready, details }: SystemInput): SystemNode[] {
  const readiness = ready.body?.status ?? (ready.status === 503 ? 'not_ready' : 'unknown');
  const { revision, head } = status.database;
  const nodes = [
    fromRaw(readiness, { id: 'api', name: 'API', role: 'Serves this console' }),
    fromRaw(status.database.status, {
      id: 'database',
      name: 'Database',
      role: 'Stores tenant data',
      detail: `revision ${revision ?? 'none'}, head ${head ?? 'none'}`,
    }),
    fromRaw(status.worker.status, {
      id: 'workers',
      name: 'Workers',
      role: 'Run queued jobs',
      detail: `${status.worker.count ?? 0} reporting`,
      lastSeenAt: status.worker.last_heartbeat_at,
    }),
  ];
  if (!details) return nodes;

  const { queued, leased, dead_in_window: dead } = details.queue;
  const telemetry = details.telemetry;
  const failedSpans = telemetry.otlp?.failed ?? 0;
  return [
    ...nodes,
    {
      id: 'queue',
      name: 'Job queue',
      role: 'Holds work for workers',
      tone: dead > 0 ? 'warn' : 'good',
      state: dead > 0 ? 'Dead jobs' : 'Clear',
      raw: `dead=${dead}`,
      detail: `${queued} queued, ${leased} leased`,
    },
    {
      id: 'traces',
      name: 'Traces',
      role: 'Exports run traces',
      tone: failedSpans > 0 ? 'warn' : telemetry.exporter === 'otlp' ? 'good' : 'idle',
      state:
        failedSpans > 0
          ? 'Export failing'
          : telemetry.exporter === 'otlp'
            ? 'Exporting'
            : 'Not exporting',
      raw: `exporter=${telemetry.exporter}`,
      detail: telemetry.otlp
        ? `${telemetry.otlp.exported} exported, ${failedSpans} failed`
        : `${telemetry.buffered_spans} spans buffered`,
    },
  ];
}

/**
 * Live workers one by one, for platform admins. Every restart leaves an old row behind, so stale
 * workers roll up into one line: the Workers node already raises the alarm if none are alive.
 */
export function buildWorkerNodes(installation?: PlatformInstallation): SystemNode[] {
  const workers = installation?.workers ?? [];
  const live = workers.filter((w) => w.status !== 'stale');
  const stale = workers.filter((w) => w.status === 'stale');
  const nodes = live.map((w) =>
    fromRaw(w.status, {
      id: `worker-${w.name}`,
      name: w.name,
      role: 'Worker process',
      lastSeenAt: w.last_seen_at,
    }),
  );
  if (stale.length === 0) return nodes;
  const newest = stale
    .map((w) => w.last_seen_at)
    .sort()
    .at(-1);
  return [
    ...nodes,
    {
      id: 'workers-stale',
      name: 'Stopped workers',
      role: 'No longer reporting',
      tone: 'idle',
      state: `${stale.length} stopped`,
      raw: `stale=${stale.length}`,
      lastSeenAt: newest,
    },
  ];
}

/** The hero headline: the condition of the system, stated outright. */
export function headline(nodes: readonly SystemNode[]): string {
  const issues = nodes.filter((n) => n.tone === 'warn');
  if (issues.length === 0) return 'Every component is carrying load.';
  if (issues.length === 1) {
    const { name } = issues[0]!;
    return `${name} ${name.endsWith('s') ? 'need' : 'needs'} attention.`;
  }
  return `${issues.length} components need attention.`;
}

// ---- Runs ----------------------------------------------------------------------------------

export function runTone(run: Pick<Run, 'status' | 'outcome_unknown'>): Tone {
  if (run.outcome_unknown) return 'warn';
  switch (run.status) {
    case 'completed':
      return 'good';
    case 'running':
    case 'pending':
    case 'waiting_for_approval':
      return 'active';
    case 'cancelled':
      return 'idle';
    default:
      return 'warn';
  }
}

export const runLabel = (run: Pick<Run, 'status' | 'outcome_unknown'>): string =>
  run.outcome_unknown ? UNKNOWN_OUTCOME_LABEL : STATUS_LABEL[run.status];

export const TONE_LABEL: Record<Tone, string> = {
  good: 'Completed',
  active: 'In progress',
  warn: 'Needs attention',
  idle: 'Cancelled',
};

export function tallyRuns(runs: readonly Run[]): Record<Tone, number> {
  const tally: Record<Tone, number> = { good: 0, active: 0, warn: 0, idle: 0 };
  for (const r of runs) tally[runTone(r)] += 1;
  return tally;
}

export function runDuration(run: Pick<Run, 'started_at' | 'finished_at'>): string {
  if (!run.started_at) return 'Not started';
  if (!run.finished_at) return 'Running';
  const seconds = (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000;
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${(seconds / 60).toFixed(1)}m`;
}

// ---- Readout -------------------------------------------------------------------------------

export interface ReadoutLine {
  key: string;
  value: string;
  tone: Tone;
}

/** Scrolling prints these in order; every value comes from the running installation. */
export function buildReadout(
  nodes: readonly SystemNode[],
  workers: readonly SystemNode[],
  runs: readonly Run[] | undefined,
): ReadoutLine[] {
  const lines: ReadoutLine[] = [...nodes, ...workers].map((n) => ({
    key: n.name,
    value: n.detail ? `${n.state}: ${n.detail}` : n.state,
    tone: n.tone,
  }));
  if (runs) {
    const t = tallyRuns(runs);
    lines.push({
      key: 'Recent runs',
      value: `${runs.length} read, ${t.active} in progress, ${t.warn} need attention`,
      tone: t.warn > 0 ? 'warn' : t.active > 0 ? 'active' : 'good',
    });
  }
  return lines;
}

// ---- Administration ------------------------------------------------------------------------

/** Task groups over the existing nav: visibleNav stays the only source of who sees what. */
const ADMIN_GROUPS: readonly { title: string; ids: readonly string[] }[] = [
  { title: 'Deploy', ids: ['build-pipelines', 'build-agents', 'integrate-connections'] },
  {
    title: 'Monitor',
    ids: ['operate-runs', 'operate-approvals', 'platform-status', 'govern-performance'],
  },
  {
    title: 'Configure',
    ids: ['platform-settings', 'integrate-providers', 'security-idps', 'integrate-egress'],
  },
];

export interface AdminGroup {
  title: string;
  items: ResolvedNavItem[];
}

export function adminGroups(org: string, opts: Parameters<typeof visibleNav>[1]): AdminGroup[] {
  const visible = new Map(visibleNav(parseScope(`/o/${org}`), opts).map((i) => [i.id, i]));
  return ADMIN_GROUPS.map(({ title, ids }) => ({
    title,
    items: ids.flatMap((id) => visible.get(id) ?? []),
  })).filter((g) => g.items.length > 0);
}

/** The Board card's link, or undefined when visibleNav hides the Board item from this user. */
export function boardCardHref(
  org: string,
  opts: Parameters<typeof visibleNav>[1],
): string | undefined {
  return visibleNav(parseScope(`/o/${org}`), opts).find((i) => i.id === 'deliver-board')?.href;
}
