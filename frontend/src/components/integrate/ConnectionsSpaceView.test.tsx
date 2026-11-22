import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TargetOut } from '@/lib/api/delivery-types';
import type { Connection, ModelProvider } from '@/lib/api/integrate-types';
import { TARGET } from '@/test/delivery';
import { ADAPTER_PLANNED, ADAPTER_REST, list, stubInt } from '@/test/integrate';
import { problemResponse, renderWithClient } from '@/test/utils';
import { ConnectionsSpaceView } from './ConnectionsSpaceView';

const nav = vi.hoisted(() => ({ query: '', replace: vi.fn(), push: vi.fn() }));
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(nav.query),
  usePathname: () => '/o/acme/connections',
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
}));
beforeEach(() => {
  nav.query = '';
  nav.replace.mockReset();
});
afterEach(() => vi.unstubAllGlobals());

const READ = ['connection:read', 'model:read', 'delivery:target:read'];
const C = 'GET /api/v1/orgs/acme/connections';
const P = 'GET /api/v1/orgs/acme/model-providers';
const T = 'GET /api/v1/orgs/acme/delivery/targets';

const CONNECTION: Connection = {
  id: 'c-1',
  name: 'Orders API',
  connection_type: 'rest',
  adapter_key: 'rest.http',
  scope: { workspace: 'eng', project: 'api' },
  config: { base_url: 'https://orders.example.test' },
  auth_method: 'bearer',
  secret_id: 's-1',
  access_mode: 'read',
  status: 'active',
  last_tested_at: '2026-11-01T10:00:00Z',
  row_version: 1,
};

const PROVIDER: ModelProvider = {
  id: 'p-1',
  name: 'Azure OpenAI',
  adapter_key: 'rest.http',
  connection_id: 'c-1',
  health_status: 'healthy',
  health_checked_at: '2026-11-02T10:00:00Z',
  row_version: 1,
};

const ADAPTERS = [{ ...ADAPTER_REST, display_name: 'REST API' }, ADAPTER_PLANNED];

function stub(
  routes: {
    connections?: Connection[];
    providers?: ModelProvider[];
    targets?: TargetOut[];
  } = {},
  perms: string[] = READ,
  extra: Parameters<typeof stubInt>[0] = {},
) {
  return stubInt(
    {
      [C]: () => list(routes.connections ?? [CONNECTION]),
      [P]: () => list(routes.providers ?? [PROVIDER]),
      [T]: () => list(routes.targets ?? [TARGET]),
      'GET /api/v1/orgs/acme/workspaces/eng/projects': () => list([]),
      ...extra,
    },
    perms,
    ADAPTERS,
  );
}

const panel = () => screen.getByRole('tabpanel');
/** Renders the space and waits for the permissions to show its tabs. */
async function renderSpace() {
  renderWithClient(<ConnectionsSpaceView />);
  await screen.findByRole('tablist');
}
const section = (name: string) => within(panel()).getByRole('region', { name });
const rowOf = (text: string) => within(panel()).getByText(text).closest('tr')!;
const listCalls = (fetchMock: ReturnType<typeof stub>, key: string) =>
  fetchMock.calls.filter((c) => c.key === key);

describe('ConnectionsSpaceView', () => {
  it('groups connections, model providers and delivery targets on All', async () => {
    stub();
    await renderSpace();
    expect(await screen.findByRole('heading', { name: 'Connections', level: 1 })).toBeVisible();
    const tabs = screen.getAllByRole('tab').map((t) => t.textContent);
    expect(tabs).toEqual(['All', 'Data and tools', 'Model providers', 'Delivery targets']);
    expect(
      await within(section('Data and tools')).findByRole('link', { name: 'Orders API' }),
    ).toHaveAttribute('href', '/o/acme/connections/c-1');
    expect(
      await within(section('Model providers')).findByRole('link', { name: 'Azure OpenAI' }),
    ).toHaveAttribute('href', '/o/acme/integrate/providers/p-1');
    expect(
      await within(section('Delivery targets')).findByRole('link', { name: 'Analytics prod' }),
    ).toHaveAttribute('href', '/o/acme/deliver/targets');
    const headings = within(panel())
      .getAllByRole('heading', { level: 2 })
      .map((h) => h.textContent);
    expect(headings).toEqual(['Data and tools', 'Model providers', 'Delivery targets']);
  });

  it('shows only its own section on a group tab', async () => {
    stub();
    await renderSpace();
    await userEvent.click(await screen.findByRole('tab', { name: 'Model providers' }));
    expect(within(panel()).getAllByRole('heading', { level: 2 })).toHaveLength(1);
    expect(await within(panel()).findByText('Azure OpenAI')).toBeInTheDocument();
    expect(within(panel()).queryByText('Orders API')).not.toBeInTheDocument();
  });

  it('hides the tabs and lists the user may not read', async () => {
    const fetchMock = stub({}, ['model:read']);
    await renderSpace();
    expect(await within(panel()).findByText('Azure OpenAI')).toBeInTheDocument();
    const tabs = screen.getAllByRole('tab').map((t) => t.textContent);
    expect(tabs).toEqual(['All', 'Model providers']);
    expect(listCalls(fetchMock, C)).toHaveLength(0);
    expect(listCalls(fetchMock, T)).toHaveLength(0);
    // The provider's connection is not loaded, so its credential is unknown.
    expect(rowOf('Azure OpenAI')).toHaveTextContent(/-$/);
  });

  it('renders NoAccess and fetches no list without any of the three read permissions', async () => {
    const fetchMock = stub({}, ['connection:manage', 'secret:read']);
    renderWithClient(<ConnectionsSpaceView />);
    expect(
      await screen.findByRole('heading', { name: 'Not found or no access' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    for (const key of [C, P, T]) expect(listCalls(fetchMock, key)).toHaveLength(0);
  });

  it('tags rows inherited by the chosen project as Shared', async () => {
    nav.query = 'ws=eng&proj=api';
    const fetchMock = stub({
      connections: [
        CONNECTION,
        { ...CONNECTION, id: 'c-2', name: 'Client CRM', scope: { workspace: null, project: null } },
      ],
      targets: [
        { ...TARGET, id: 't-2', name: 'Eng shared', project: null },
        { ...TARGET, id: 't-3', name: 'Other workspace', workspace: 'ops', project: null },
        { ...TARGET, id: 't-4', name: 'Other project', project: 'web' },
      ],
    });
    await renderSpace();
    await within(panel()).findByText('Client CRM');
    const url = listCalls(fetchMock, C)[0]!.url;
    expect(url).toContain('project=api');
    expect(url).toContain('include_shared=true');
    expect(url).toContain('workspace=eng');

    const shared = rowOf('Client CRM');
    expect(within(shared).getByText('Shared')).toBeInTheDocument();
    expect(shared).toHaveTextContent('Shared: client-wide');
    const own = rowOf('Orders API');
    expect(within(own).queryByText('Shared')).not.toBeInTheDocument();
    expect(own).toHaveTextContent('Project api');

    await within(panel()).findByText('Eng shared');
    const target = rowOf('Eng shared');
    expect(within(target).getByText('Shared')).toBeInTheDocument();
    expect(target).toHaveTextContent('Shared: workspace eng');
    expect(within(panel()).queryByText('Other workspace')).not.toBeInTheDocument();
    expect(within(panel()).queryByText('Other project')).not.toBeInTheDocument();

    expect(await within(panel()).findByText('Azure OpenAI')).toBeInTheDocument();
    expect(rowOf('Azure OpenAI')).toHaveTextContent('Shared: client-wide');
  });

  it('shows no Shared tag and asks for no shared rows without a project', async () => {
    const fetchMock = stub({
      connections: [
        CONNECTION,
        { ...CONNECTION, id: 'c-2', name: 'Client CRM', scope: { workspace: null, project: null } },
      ],
      targets: [{ ...TARGET, project: null }],
    });
    await renderSpace();
    await within(panel()).findByText('Client CRM');
    await within(panel()).findByText('Analytics prod');
    await within(panel()).findByText('Azure OpenAI');
    expect(within(panel()).queryByText('Shared')).not.toBeInTheDocument();
    expect(rowOf('Client CRM')).toHaveTextContent('Organization');
    expect(rowOf('Analytics prod')).toHaveTextContent('Workspace eng');
    const url = listCalls(fetchMock, C)[0]!.url;
    expect(url).not.toContain('include_shared');
  });

  it('writes a scope change into the query string', async () => {
    stub({}, READ, {
      'GET /api/v1/orgs/acme/workspaces': () =>
        list([{ id: 'w-1', slug: 'eng', name: 'Engineering', status: 'active' }]),
    });
    await renderSpace();
    await screen.findByRole('option', { name: 'Engineering' });
    await userEvent.selectOptions(screen.getByLabelText('Workspace'), 'eng');
    expect(nav.replace).toHaveBeenCalledWith('/o/acme/connections?ws=eng');
  });

  it('shows a loading status while a list is pending', async () => {
    stub({}, READ, { [C]: () => new Promise<Response>(() => {}) });
    await renderSpace();
    expect(await within(section('Data and tools')).findByRole('status')).toHaveTextContent(
      'Loading',
    );
  });

  it('shows the empty state of an empty group', async () => {
    stub({ connections: [] });
    await renderSpace();
    expect(
      await within(panel()).findByText('No data and tools connections in this scope.'),
    ).toBeInTheDocument();
  });

  it('shows the error of a failed list', async () => {
    stub({}, READ, { [C]: () => problemResponse(500, 'internal_error', { title: 'Boom' }) });
    await renderSpace();
    expect(await within(section('Data and tools')).findByRole('alert')).toBeInTheDocument();
  });

  it('shows NoAccess in a 403 section while the other sections still render', async () => {
    stub({}, READ, { [C]: () => problemResponse(403, 'forbidden') });
    await renderSpace();
    expect(
      await within(section('Data and tools')).findByRole('heading', {
        name: 'Not found or no access',
      }),
    ).toBeInTheDocument();
    expect(await within(panel()).findByText('Azure OpenAI')).toBeInTheDocument();
    expect(await within(panel()).findByText('Analytics prod')).toBeInTheDocument();
  });

  it('renders the last test, credential and kind columns', async () => {
    stub({
      connections: [
        { ...CONNECTION, id: 'c-1', name: 'Never tested', last_tested_at: null, secret_id: null },
        { ...CONNECTION, id: 'c-2', name: 'Passed', last_test_outcome: 'pass' },
        { ...CONNECTION, id: 'c-3', name: 'No outcome' },
      ],
    });
    await renderSpace();
    await within(panel()).findByText('Never tested');
    const never = rowOf('Never tested');
    expect(never).toHaveTextContent('never');
    expect(never.cells[5]).toHaveTextContent(/^not set$/);
    const passed = rowOf('Passed');
    expect(within(passed).getByText('pass')).toBeInTheDocument();
    expect(passed.cells[5]).toHaveTextContent(/^set$/);
    const noOutcome = rowOf('No outcome');
    const date = new Date('2026-11-01T10:00:00Z').toLocaleString();
    expect(noOutcome.cells[4]).toHaveTextContent(new RegExp(`^${date}$`));
    expect(noOutcome.cells[1]).toHaveTextContent('REST API');
    expect(within(noOutcome.cells[1] as HTMLElement).getByText('Validated')).toBeInTheDocument();

    await within(panel()).findByText('Analytics prod');
    const target = rowOf('Analytics prod');
    expect(target).toHaveTextContent('held by executor');
    expect(target.cells[1]).toHaveTextContent(/^databricks$/);
    expect(target.cells[4]).toHaveTextContent(/^-$/);
    const provider = rowOf('Azure OpenAI');
    // Its connection c-1 is loaded here without a secret.
    expect(provider.cells[5]).toHaveTextContent(/^not set$/);
  });

  it('links a Databricks connection and a target on the same workspace host', async () => {
    const host = 'https://adb-1.azuredatabricks.net';
    stub({
      connections: [
        {
          ...CONNECTION,
          id: 'c-9',
          name: 'Lakehouse',
          adapter_key: 'databricks',
          config: { host },
        },
        {
          ...CONNECTION,
          id: 'c-8',
          name: 'Other lake',
          adapter_key: 'databricks',
          config: { host: 'https://adb-2.azuredatabricks.net' },
        },
      ],
      targets: [
        { ...TARGET, name: 'prod', address: host },
        { ...TARGET, id: 't-7', name: 'dev', address: 'https://adb-3.azuredatabricks.net' },
      ],
    });
    await renderSpace();
    await within(panel()).findByText('Lakehouse');
    await within(panel()).findByText('prod');
    await waitFor(() =>
      expect(rowOf('Lakehouse')).toHaveTextContent('Same workspace as target prod'),
    );
    expect(rowOf('prod')).toHaveTextContent('Same workspace as connection Lakehouse');
    expect(rowOf('Other lake')).not.toHaveTextContent('Same workspace');
    expect(rowOf('dev')).not.toHaveTextContent('Same workspace');
    expect(within(rowOf('Lakehouse')).getByText('Declared — not validated')).toBeInTheDocument();
  });

  it('offers Create connection with connection:manage', async () => {
    stub({}, [...READ, 'connection:manage']);
    await renderSpace();
    expect(await screen.findByRole('button', { name: 'Create connection' })).toBeInTheDocument();
  });

  it('has no Create connection button without connection:manage', async () => {
    stub();
    await renderSpace();
    await within(panel()).findByText('Orders API');
    expect(screen.queryByRole('button', { name: 'Create connection' })).not.toBeInTheDocument();
  });
});
