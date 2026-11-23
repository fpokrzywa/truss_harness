import type { BoardOut, BoardProject, BoardTarget } from '@/lib/api/board-types';
import { ORG, RELEASE_READ_PERMS, WORKSPACES } from './releases';
import { ALL_PERMS, stubApi } from './security';
import { jsonResponse, type Handler } from './utils';

export const BOARD = `${ORG}/board`;

const target = (
  target_id: string,
  name: string,
  environment: string,
  cell: Partial<Pick<BoardTarget, 'current' | 'last_attempt' | 'open_plan'>> = {},
): BoardTarget => ({
  target_id,
  name,
  environment,
  platform_kind: 'databricks',
  current: null,
  last_attempt: null,
  open_plan: null,
  ...cell,
});

/**
 * eng/api: two solutions over targets in dev and prod, given out of order. Sales assistant is
 * verified at v3 on Zeta prod, has an unknown outcome and an approved plan on Analytics dev and
 * nothing on Analytics prod; the archived Billing bot failed on Analytics dev with an expired plan.
 */
export const PROJECT: BoardProject = {
  workspace: { id: 'ws-1', slug: 'eng', name: 'Engineering' },
  project: { id: 'p-1', slug: 'api', name: 'API' },
  truncated: false,
  solutions: [
    {
      id: 's-1',
      name: 'Sales assistant',
      status: 'active',
      latest_version_no: 3,
      releases: [
        {
          id: 'r-3',
          version_no: 3,
          digest12: 'abcdef012345',
          status: 'sealed',
          sealed_at: '2026-11-20T10:00:00Z',
          evaluation: { run_id: 'ev-3', status: 'passed' },
        },
        {
          id: 'r-2',
          version_no: 2,
          digest12: '0123456789ab',
          status: 'withdrawn',
          sealed_at: '2026-11-19T10:00:00Z',
          evaluation: null,
        },
      ],
      targets: [
        target('t-zeta', 'Zeta prod', 'prod', {
          current: {
            release_id: 'r-3',
            version_no: 3,
            deployment_id: 'd-3',
            verified_at: '2026-11-21T10:00:00Z',
          },
          last_attempt: {
            deployment_id: 'd-3',
            release_id: 'r-3',
            status: 'verified',
            status_reason: null,
            updated_at: '2026-11-21T10:00:00Z',
          },
        }),
        target('t-dev', 'Analytics dev', 'dev', {
          last_attempt: {
            deployment_id: 'd-4',
            release_id: 'r-3',
            status: 'unknown_outcome',
            status_reason: null,
            updated_at: '2026-11-22T10:00:00Z',
          },
          open_plan: {
            plan_id: 'pl-1',
            release_id: 'r-3',
            status: 'approved',
            expires_at: '2026-11-25T10:00:00Z',
          },
        }),
        target('t-prod', 'Analytics prod', 'prod'),
      ],
    },
    {
      id: 's-2',
      name: 'Billing bot',
      status: 'archived',
      latest_version_no: null,
      releases: [],
      targets: [
        target('t-dev', 'Analytics dev', 'dev', {
          last_attempt: {
            deployment_id: 'd-5',
            release_id: 'r-9',
            status: 'failed',
            status_reason: 'warehouse unreachable',
            updated_at: '2026-11-22T11:00:00Z',
          },
          open_plan: { plan_id: 'pl-2', release_id: 'r-9', status: 'expired', expires_at: null },
        }),
      ],
    },
  ],
};

/** eng/web: cut short by the server, and without solutions. */
export const TRUNCATED: BoardProject = {
  workspace: { id: 'ws-1', slug: 'eng', name: 'Engineering' },
  project: { id: 'p-2', slug: 'web', name: 'Web' },
  truncated: true,
  solutions: [],
};

export const board = (projects: BoardProject[], next_cursor: string | null = null): BoardOut => ({
  generated_at: '2026-11-23T10:00:00Z',
  projects,
  next_cursor,
});

/** stubApi plus the eng workspace and a board of PROJECT. */
export function stubBoard(
  routes: Record<string, Handler> = {},
  perms: string[] = [...ALL_PERMS, ...RELEASE_READ_PERMS],
) {
  return stubApi(
    {
      ...WORKSPACES,
      [`GET ${BOARD}`]: () => jsonResponse(board([PROJECT])),
      ...routes,
    },
    perms,
  );
}
