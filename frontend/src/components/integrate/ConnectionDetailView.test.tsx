import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { stubInt } from '@/test/integrate';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { ConnectionDetailView } from './ConnectionDetailView';
import { ConnectionTestsView } from './ConnectionTests';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const BASE = {
  id: 'c-1',
  name: 'Orders API',
  connection_type: 'rest',
  adapter_key: 'rest.http',
  scope: { workspace: 'eng' },
  config: { base_url: 'https://orders.example.test' },
  auth_method: 'bearer',
  secret_id: 'sec-1',
  egress_policy_id: null,
  access_mode: 'read',
  status: 'draft',
  row_version: 5,
};
const route = (c: object) => ({
  'GET /api/v1/orgs/acme/connections/c-1': () => jsonResponse({ ...BASE, ...c }),
  'GET /api/v1/orgs/acme/secrets': () => list([{ id: 'sec-1', name: 'orders-token', version: 2 }]),
  'GET /api/v1/orgs/acme/connections/c-1/discoveries/latest': () =>
    problemResponse(404, 'not_found'),
});

const PASS = {
  id: 't-1',
  outcome: 'pass',
  duration_ms: 42,
  checks: [
    { name: 'capability', status: 'pass', detail: 'GET / returned 200' },
    { name: 'config', status: 'pass', detail: 'valid' },
    { name: 'egress', status: 'pass', detail: 'allowed by policy' },
    { name: 'dns', status: 'pass', detail: 'resolved 127.0.0.1' },
    { name: 'tls', status: 'skipped', detail: 'plain http' },
    { name: 'auth', status: 'pass', detail: 'bearer accepted' },
  ],
};

// ARCH M2: the POST queues the test (202); the result comes from the job's status route
const QUEUED = {
  id: 'j-1',
  connection_id: 'c-1',
  status: 'queued',
  result: null,
  error: null,
  created_at: '2026-11-18T10:00:00Z',
};

describe('ConnectionDetailView', () => {
  it('shows config and the secret by name only, with activation messaging', async () => {
    stubInt(route({}));
    renderWithClient(<ConnectionDetailView id="c-1" />);
    expect(await screen.findByText('https://orders.example.test')).toBeInTheDocument();
    expect(await screen.findByText(/using secret orders-token/)).toBeInTheDocument();
    expect(
      screen.getByText(/Activation requires a passing test within the last 24 hours/),
    ).toBeInTheDocument();
  });

  it('sends the row_version when activating and surfaces a server refusal', async () => {
    const fetchMock = stubInt({
      ...route({}),
      'POST /api/v1/orgs/acme/connections/c-1/status': () =>
        problemResponse(409, 'test_required', {
          detail: 'A passing test within 24 h is required.',
        }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Activate' }));
    expect(await screen.findByText('A passing test within 24 h is required.')).toBeInTheDocument();
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/connections/c-1/status')).toEqual({
      status: 'active',
      row_version: 5,
    });
  });

  it('requires confirmation before retiring', async () => {
    const fetchMock = stubInt({
      ...route({ status: 'active' }),
      'POST /api/v1/orgs/acme/connections/c-1/status': () =>
        jsonResponse({ ...BASE, status: 'retired' }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retire' }));
    expect(fetchMock.calls.some((c) => c.key.endsWith('/status'))).toBe(false);
    await userEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Retire connection' }),
    );
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/connections/c-1/status')).toEqual({
        status: 'retired',
        row_version: 5,
      }),
    );
  });

  it('renders per-check detail of a test run', async () => {
    stubInt({
      ...route({}),
      'POST /api/v1/orgs/acme/connections/c-1/test': () => jsonResponse(QUEUED, { status: 202 }),
      'GET /api/v1/orgs/acme/connections/c-1/test-jobs/j-1': () =>
        jsonResponse({ ...QUEUED, status: 'done', result: PASS }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Test connection' }));
    const table = await screen.findByRole(
      'table',
      { name: 'Checks for this test run' },
      { timeout: 5000 },
    );
    const names = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((r) => within(r).getAllByRole('cell')[0]?.textContent);
    expect(names).toEqual(['config', 'egress', 'dns', 'tls', 'auth', 'capability']);
    expect(within(table).getByText('bearer accepted')).toBeInTheDocument();
    expect(within(table).getByText('skipped')).toBeInTheDocument();
  });

  it('shows why a queued test did not run', async () => {
    stubInt({
      ...route({}),
      'POST /api/v1/orgs/acme/connections/c-1/test': () => jsonResponse(QUEUED, { status: 202 }),
      'GET /api/v1/orgs/acme/connections/c-1/test-jobs/j-1': () =>
        jsonResponse({ ...QUEUED, status: 'failed', error: 'forbidden' }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Test connection' }));
    expect(
      await screen.findByText(/The connection test did not run \(forbidden\)/, undefined, {
        timeout: 5000,
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: 'Checks for this test run' })).toBeNull();
  });

  it('disables test, discovery and the console for a planned adapter and says why', async () => {
    stubInt(route({ adapter_key: 'databricks', status: 'active' }));
    renderWithClient(<ConnectionDetailView id="c-1" />);
    expect(await screen.findByRole('button', { name: 'Test connection' })).toBeDisabled();
    expect(screen.getAllByText(/declared only/).length).toBeGreaterThan(0);
    expect(screen.getByText('Declared — not validated')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run discovery' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled();
  });

  it('shows the latest discovery: paths, tools, and tables with columns', async () => {
    stubInt({
      ...route({}),
      'GET /api/v1/orgs/acme/connections/c-1/discoveries/latest': () =>
        jsonResponse({
          created_at: '2026-05-01T10:00:00Z',
          result: {
            paths: { '/orders': { get: {} } },
            tools: [{ name: 'echo', description: 'Echo' }],
            tables: [
              { schema: 'public', name: 'customers', columns: [{ name: 'id', type: 'uuid' }] },
            ],
          },
        }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    expect(await screen.findByText('/orders')).toBeInTheDocument();
    expect(screen.getByText('echo')).toBeInTheDocument();
    expect(screen.getByText('public.customers')).toBeInTheDocument();
    expect(screen.getByText(/id uuid/)).toBeInTheDocument();
  });

  it('queues a discovery, polls its job and renders the recorded result', async () => {
    const DISCOVERY = {
      id: 'd-1',
      kind: 'openapi',
      discovered_at: '2026-11-22T10:00:00Z',
      payload: { paths: [{ method: 'GET', path: '/invoices', summary: 'list' }] },
    };
    let recorded = false;
    const fetchMock = stubInt({
      ...route({}),
      'GET /api/v1/orgs/acme/connections/c-1/discoveries/latest': () =>
        recorded ? jsonResponse(DISCOVERY) : problemResponse(404, 'not_found'),
      'POST /api/v1/orgs/acme/connections/c-1/discover': () =>
        jsonResponse(QUEUED, { status: 202 }),
      'GET /api/v1/orgs/acme/connections/c-1/discovery-jobs/j-1': () => {
        recorded = true;
        return jsonResponse({ ...QUEUED, status: 'done', result: DISCOVERY });
      },
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    expect(
      await screen.findByText('No discovery has been run for this connection.'),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Run discovery' }));
    expect(await screen.findByText('/invoices', undefined, { timeout: 5000 })).toBeInTheDocument();
    expect(
      fetchMock.calls.some((c) =>
        c.key.endsWith('GET /api/v1/orgs/acme/connections/c-1/discovery-jobs/j-1'),
      ),
    ).toBe(true);
  });

  it('shows why a queued discovery did not run', async () => {
    stubInt({
      ...route({}),
      'POST /api/v1/orgs/acme/connections/c-1/discover': () =>
        jsonResponse(QUEUED, { status: 202 }),
      'GET /api/v1/orgs/acme/connections/c-1/discovery-jobs/j-1': () =>
        jsonResponse({ ...QUEUED, status: 'failed', error: 'validation_error' }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Run discovery' }));
    expect(
      await screen.findByText(
        /The connection discovery did not run \(validation_error\)/,
        undefined,
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('No discovery has been run for this connection.')).toBeInTheDocument();
  });

  it('hides the test and console sections without the permissions', async () => {
    stubInt(route({ status: 'active' }), ['connection:read']);
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await screen.findByText('https://orders.example.test');
    expect(screen.queryByRole('button', { name: 'Test connection' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Console' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Activate' })).not.toBeInTheDocument();
  });
});

describe('Connection console', () => {
  it('sends a REST invocation and shows the truncated flag', async () => {
    const fetchMock = stubInt({
      ...route({ status: 'active' }),
      'POST /api/v1/orgs/acme/connections/c-1/invoke': () =>
        jsonResponse({ result: { items: [1] }, truncated: true, duration_ms: 12 }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.type(await screen.findByLabelText('Path'), '/orders');
    await userEvent.type(screen.getByLabelText('Query (JSON object)'), '{{"limit":1}');
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText(/Truncated/)).toBeInTheDocument();
    expect(screen.getByText(/Completed in 12 ms/)).toBeInTheDocument();
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/connections/c-1/invoke')).toEqual({
      method: 'GET',
      path: '/orders',
      query: { limit: 1 },
    });
  });

  it('rejects invalid JSON locally', async () => {
    const fetchMock = stubInt(route({ status: 'active' }));
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.type(await screen.findByLabelText('Path'), '/x');
    await userEvent.type(screen.getByLabelText('Body (JSON)'), 'not json');
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Body must be valid JSON.');
    expect(fetchMock.calls.some((c) => c.key.endsWith('/invoke'))).toBe(false);
  });

  it('shows the database read-only notice and sends sql with params', async () => {
    const fetchMock = stubInt({
      ...route({ connection_type: 'database', adapter_key: 'postgres', status: 'active' }),
      'GET /api/v1/adapters': () =>
        list([
          {
            key: 'postgres',
            kind: 'database',
            version: '1',
            capabilities: [{ capability: 'query', maturity: 'validated' }],
            config_schema: { properties: {} },
            secret_fields: [],
            auth_methods: ['password'],
          },
        ]),
      'POST /api/v1/orgs/acme/connections/c-1/invoke': () =>
        jsonResponse({ result: { rows: [] }, truncated: false, duration_ms: 3 }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    expect(
      await screen.findByText(/Read-only: statements run in a READ ONLY transaction/),
    ).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('SQL'), 'select 1');
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/connections/c-1/invoke')).toEqual({
        sql: 'select 1',
        params: [],
      }),
    );
    expect(screen.queryByText(/Truncated/)).not.toBeInTheDocument();
  });

  it('is disabled with an explanation when the connection is not active', async () => {
    stubInt(route({ status: 'disabled' }));
    renderWithClient(<ConnectionDetailView id="c-1" />);
    expect(await screen.findByText(/The connection is disabled/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled();
  });
});

describe('ConnectionTestsView', () => {
  it('lists history with each run per-check detail', async () => {
    stubInt({
      ...route({}),
      'GET /api/v1/orgs/acme/connections/c-1/tests': () =>
        list([
          PASS,
          {
            id: 't-0',
            outcome: 'fail',
            error_class: 'egress_denied',
            duration_ms: 5,
            checks: [{ name: 'egress', status: 'fail', detail: 'private address denied' }],
          },
        ]),
    });
    renderWithClient(<ConnectionTestsView id="c-1" />);
    const history = await screen.findByRole('list', { name: 'Test history' });
    expect(within(history).getAllByRole('listitem')).toHaveLength(2);
    expect(within(history).getByText('private address denied')).toBeInTheDocument();
    expect(within(history).getByText('egress_denied')).toBeInTheDocument();
  });
});
