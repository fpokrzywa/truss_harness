import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { stubInt } from '@/test/integrate';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { AdaptersView } from './AdaptersView';
import { ConnectionsView } from './ConnectionsView';
import { EgressView, PlatformEgressView } from './EgressView';

vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useRouter: () => ({ push: vi.fn() }),
}));
afterEach(() => vi.unstubAllGlobals());

describe('AdaptersView', () => {
  it('shows maturity per adapter and capability; planned adapters read "Declared — not validated"', async () => {
    stubInt();
    renderWithClient(<AdaptersView />);
    const planned = (await screen.findByRole('heading', { name: /databricks/ })).closest(
      'section',
    )!;
    expect(within(planned).getAllByText('Declared — not validated').length).toBeGreaterThanOrEqual(
      3,
    );
    expect(within(planned).queryByText('Validated')).not.toBeInTheDocument();
    const rest = screen.getByRole('heading', { name: /rest\.http/ }).closest('section')!;
    expect(within(rest).getAllByText('Validated').length).toBeGreaterThanOrEqual(1);
    expect(within(rest).getByText(/Suite 2\.1/)).toBeInTheDocument();
  });

  it('loads conformance run evidence on demand', async () => {
    stubInt({
      'GET /api/v1/adapters/rest.http/conformance-runs': () =>
        list([
          {
            id: 'run-1',
            suite_version: '2.1',
            adapter_version: '1.0.0',
            finished_at: '2026-05-01T10:00:00Z',
            outcome: 'pass',
          },
        ]),
      'GET /api/v1/adapters/databricks/conformance-runs': () => list([]),
    });
    renderWithClient(<AdaptersView />);
    await userEvent.click(
      await screen.findByRole('button', { name: /Show conformance runs for rest\.http/ }),
    );
    const table = await screen.findByRole('table', { name: 'Conformance runs for rest.http' });
    expect(within(table).getByText('2.1')).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole('button', { name: /Show conformance runs for databricks/ }),
    );
    expect(await screen.findByText(/nothing here is validated/)).toBeInTheDocument();
  });
});

describe('ConnectionsView', () => {
  const CONN = {
    id: 'c-1',
    name: 'Orders API',
    connection_type: 'rest',
    adapter_key: 'rest.http',
    scope: { workspace: 'eng' },
    config: {},
    auth_method: 'none',
    access_mode: 'read',
    status: 'active',
    health: 'healthy',
    row_version: 1,
  };

  it('lists connections with status, health and adapter maturity, and passes filters', async () => {
    const fetchMock = stubInt({
      'GET /api/v1/orgs/acme/connections': () =>
        list([
          CONN,
          {
            ...CONN,
            id: 'c-2',
            name: 'Lakehouse',
            adapter_key: 'databricks',
            status: 'draft',
            health: null,
          },
        ]),
    });
    renderWithClient(<ConnectionsView />);
    const row = (await screen.findByRole('cell', { name: 'Orders API' })).closest('tr')!;
    expect(within(row).getByText('active')).toBeInTheDocument();
    expect(within(row).getByText('healthy')).toBeInTheDocument();
    expect(within(row).getByText('Validated')).toBeInTheDocument();
    const lake = screen.getByRole('cell', { name: 'Lakehouse' }).closest('tr')!;
    expect(within(lake).getByText('Declared — not validated')).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Type'), 'database');
    await userEvent.selectOptions(screen.getByLabelText('Status'), 'active');
    const last = fetchMock.calls.filter((c) => c.key === 'GET /api/v1/orgs/acme/connections').pop();
    await vi.waitFor(() => {
      const u = fetchMock.calls
        .filter((c) => c.key === 'GET /api/v1/orgs/acme/connections')
        .pop()?.url;
      expect(u).toContain('type=database');
      expect(u).toContain('status=active');
    });
    expect(last).toBeDefined();
  });

  it('hides Create connection without connection:manage', async () => {
    stubInt({ 'GET /api/v1/orgs/acme/connections': () => list([]) }, ['connection:read']);
    renderWithClient(<ConnectionsView />);
    await screen.findByText('No connections match.');
    expect(screen.queryByRole('button', { name: 'Create connection' })).not.toBeInTheDocument();
  });
});

describe('Egress policy admin', () => {
  it('creates a policy, validating ports and sending list fields as arrays', async () => {
    const fetchMock = stubInt({
      'GET /api/v1/orgs/acme/egress-policies': () => list([]),
      'POST /api/v1/orgs/acme/egress-policies': () => jsonResponse({ id: 'e-1' }, { status: 201 }),
    });
    renderWithClient(<EgressView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create policy' }));
    const dlg = screen.getByRole('dialog');
    await userEvent.type(within(dlg).getByLabelText('Name'), 'Corp only');
    await userEvent.type(within(dlg).getByLabelText('Allowed ports'), '443{Enter}99999');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Save policy' }));
    expect(within(dlg).getByText(/"99999" is not a valid port/)).toBeInTheDocument();
    await userEvent.clear(within(dlg).getByLabelText('Allowed ports'));
    await userEvent.type(within(dlg).getByLabelText('Allowed ports'), '443');
    await userEvent.type(within(dlg).getByLabelText('Allowed schemes'), 'https');
    await userEvent.type(within(dlg).getByLabelText('Allowed hosts'), 'api.example.test');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Save policy' }));
    await vi.waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/egress-policies')).toEqual({
        name: 'Corp only',
        allowed_schemes: ['https'],
        allowed_ports: [443],
        allowed_hosts: ['api.example.test'],
        allowed_cidrs: [],
        allow_private: false,
        allow_redirects: false,
      }),
    );
  });

  it('does not request or show policies without egress:manage', async () => {
    const fetchMock = stubInt({}, ['vendor:read']);
    renderWithClient(<EgressView />);
    expect(
      await screen.findByText(/do not have permission to manage egress policies/),
    ).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.includes('egress-policies'))).toBe(false);
    expect(screen.queryByRole('button', { name: 'Create policy' })).not.toBeInTheDocument();
  });

  it('platform default egress loads and saves via PUT', async () => {
    const fetchMock = stubInt({
      'GET /api/v1/platform/egress-default': () =>
        jsonResponse({ allowed_schemes: ['https'], allowed_ports: [443], allow_private: false }),
      'PUT /api/v1/platform/egress-default': () => jsonResponse({}),
    });
    renderWithClient(<PlatformEgressView />);
    await userEvent.click(await screen.findByLabelText('Allow private network addresses'));
    await userEvent.click(screen.getByRole('button', { name: 'Save default policy' }));
    expect(await screen.findByText('Default policy saved.')).toBeInTheDocument();
    expect(bodyOf(fetchMock, 'PUT /api/v1/platform/egress-default')).toMatchObject({
      allowed_schemes: ['https'],
      allowed_ports: [443],
      allow_private: true,
    });
  });
});
