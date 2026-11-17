'use client';

import { useQuery } from '@tanstack/react-query';
import { useParams } from 'next/navigation';
import type { ReactNode } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import type { Run } from '@/lib/api/operate-types';
import {
  adminGroups,
  buildNodes,
  buildReadout,
  buildWorkerNodes,
  headline,
  runTone,
  tallyRuns,
  type AdminGroup,
  type SystemInput,
} from '@/lib/command';
import { keys, usePermissions, useSession } from '@/lib/hooks';
import { AdminPanel } from './AdminPanel';
import { NetworkMatrix } from './NetworkMatrix';
import { RunTelemetry } from './RunTelemetry';
import { StartupReadout } from './StartupReadout';
import { Tabs } from './Tabs';
import { Truss } from './TrussHero';

/** Long enough not to load the API, short enough to notice a stopped worker. */
const REFRESH_MS = 5000;
const RUNS_SHOWN = 25;

function Panel({ intro, children }: { intro: string; children: ReactNode }) {
  return (
    <>
      <p className="mb-3 max-w-[70ch] text-sm text-shi-tint">{intro}</p>
      {children}
    </>
  );
}

export function CommandCenterContent({
  org,
  system,
  runs,
  runsUnavailable,
  admin,
  updatedAt,
}: {
  org: string;
  system: SystemInput;
  runs: readonly Run[] | undefined;
  runsUnavailable?: string;
  admin: readonly AdminGroup[];
  updatedAt: number;
}) {
  const nodes = buildNodes(system);
  const workers = buildWorkerNodes(system.installation);
  const flowing =
    (system.details?.queue.leased ?? 0) > 0 || (runs ?? []).some((r) => runTone(r) === 'active');
  const { status, details } = system;

  const allNodes = [...nodes, ...workers];
  const runsNeedAttention = !!runs && tallyRuns(runs).warn > 0;

  return (
    <div className="@container">
      <section aria-labelledby="condition">
        <h1 id="condition" className="font-lora text-4xl leading-tight font-medium">
          {headline(nodes)}
        </h1>
        <p className="mt-1 text-shi-tint">
          Version <span className="font-roboto-mono">{status.version}</span> in {status.environment}
          . Updated{' '}
          <time dateTime={new Date(updatedAt).toISOString()}>
            {new Date(updatedAt).toLocaleTimeString()}
          </time>
          .
        </p>
      </section>

      {/* Follows the panel's own width, so resizing the sidebar reflows this row too. */}
      <div className="mt-6 grid items-start gap-8 @5xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <div className="mx-auto w-full max-w-2xl">
          <Truss nodes={nodes} flowing={flowing} />
        </div>
        <Tabs
          label="System details"
          tabs={[
            {
              id: 'startup',
              label: 'Startup check',
              content: (
                <Panel intro="Each line is read from this installation.">
                  <StartupReadout lines={buildReadout(nodes, workers, runs)} />
                </Panel>
              ),
            },
            {
              id: 'telemetry',
              label: 'Deployment telemetry',
              alert: runsNeedAttention || (details?.queue.dead_in_window ?? 0) > 0,
              content: (
                <Panel intro="Recent runs in this organization and the jobs behind them. Refreshes every 5 seconds.">
                  <RunTelemetry
                    org={org}
                    runs={runs}
                    runsUnavailable={runsUnavailable}
                    details={details}
                  />
                </Panel>
              ),
            },
            {
              id: 'network',
              label: 'Network status',
              alert: allNodes.some((n) => n.tone === 'warn'),
              content: (
                <Panel
                  intro={
                    details
                      ? 'Every connected component and worker, with the value it reported.'
                      : 'Core components, with the value each reported. Queue, trace and per-worker rows are visible to platform administrators.'
                  }
                >
                  <NetworkMatrix nodes={allNodes} />
                </Panel>
              ),
            },
          ]}
        />
      </div>

      <section aria-labelledby="administration" className="mt-6">
        <h2 id="administration" className="mb-2 font-lora text-2xl font-medium">
          Administration
        </h2>
        <AdminPanel groups={admin} />
      </section>
    </div>
  );
}

function runsMessage(error: unknown): string | undefined {
  if (!error) return undefined;
  if (error instanceof ApiError && (error.status === 403 || error.status === 404))
    return 'This account cannot read runs in this organization. Ask an organization admin for access.';
  return 'Recent runs could not be read. Showing the last known state; retrying every 5 seconds.';
}

/** Live data for the command center. Admin-only sources simply stay absent for other roles. */
export function CommandCenter() {
  const org = decodeURIComponent(useParams<{ org: string }>().org);
  const session = useSession();
  const permissions = usePermissions(org);
  const live = { refetchInterval: REFRESH_MS };

  const status = useQuery({
    queryKey: keys.platformStatus,
    queryFn: () => endpoints.platformStatus(),
    ...live,
  });
  const ready = useQuery({ queryKey: keys.ready, queryFn: () => endpoints.ready(), ...live });
  const details = useQuery({
    queryKey: ['platform', 'health-details'],
    queryFn: () => endpoints.platformHealthDetails(),
    enabled: !!session.data?.user.is_platform_admin,
    retry: false,
    ...live,
  });
  const installation = useQuery({
    queryKey: ['platform', 'installation'],
    queryFn: () => endpoints.platformInstallation(),
    enabled: !!session.data?.user.is_platform_admin,
    retry: false,
    ...live,
  });
  const runs = useQuery({
    queryKey: ['operate', org, 'command-runs'],
    queryFn: () => endpoints.listRuns(org, {}, undefined, RUNS_SHOWN),
    retry: false,
    // A refusal will not change on its own: stop asking.
    refetchInterval: (q) =>
      q.state.error instanceof ApiError && q.state.error.status < 500 ? false : REFRESH_MS,
  });

  if (status.isError || ready.isError) {
    return (
      <div role="alert">
        <h1 className="font-lora text-4xl leading-tight font-medium">
          Platform status could not be read.
        </h1>
        <p className="mt-6 max-w-[65ch] text-lg text-shi-tint">
          The API did not answer. Check that it is running, then try again.
        </p>
        <button
          type="button"
          className="cc-admin-button mt-8"
          onClick={() => {
            void status.refetch();
            void ready.refetch();
          }}
        >
          Try again
        </button>
      </div>
    );
  }
  if (!status.data || !ready.data) {
    return (
      <h1 role="status" className="font-lora text-4xl leading-tight font-medium">
        Reading system status...
      </h1>
    );
  }

  return (
    <CommandCenterContent
      org={org}
      system={{
        status: status.data,
        ready: ready.data,
        details: details.data,
        installation: installation.data,
      }}
      runs={runs.data?.items}
      runsUnavailable={runsMessage(runs.error)}
      admin={adminGroups(org, {
        isPlatformAdmin: !!session.data?.user.is_platform_admin,
        permissions: permissions.data,
        features: session.data?.features,
      })}
      updatedAt={status.dataUpdatedAt}
    />
  );
}
