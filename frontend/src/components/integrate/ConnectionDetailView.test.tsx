import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { INT_PERMS, stubInt } from '@/test/integrate';
import { ALL_PERMS, bodyOf, list } from '@/test/security';
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

// issue #190: the POST queues the invocation (202); the outcome comes from its status route
const INVOCATION = {
  id: 'i-1',
  connection_id: 'c-1',
  status: 'queued',
  result: null,
  error: null,
  created_at: '2026-11-22T10:00:00Z',
  finished_at: null,
};
const INVOCATION_URL = 'GET /api/v1/orgs/acme/connections/c-1/invocations/i-1';

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
        jsonResponse(INVOCATION, { status: 202 }),
      [INVOCATION_URL]: () =>
        jsonResponse({
          ...INVOCATION,
          status: 'succeeded',
          result: { result: { items: [1] }, truncated: true, duration_ms: 12 },
          finished_at: '2026-11-22T10:00:01Z',
        }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.type(await screen.findByLabelText('Path'), '/orders');
    await userEvent.type(screen.getByLabelText('Query (JSON object)'), '{{"limit":1}');
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText('Invocation queued...')).toBeInTheDocument();
    expect(await screen.findByText(/Truncated/, {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText('Invocation queued...')).not.toBeInTheDocument();
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
            auth_only_fields: {},
            destination_fields: ['host', 'port', 'sslmode'],
          },
        ]),
      'POST /api/v1/orgs/acme/connections/c-1/invoke': () =>
        jsonResponse(INVOCATION, { status: 202 }),
      [INVOCATION_URL]: () =>
        jsonResponse({
          ...INVOCATION,
          status: 'succeeded',
          result: { result: { rows: [] }, truncated: false, duration_ms: 3 },
          finished_at: '2026-11-22T10:00:01Z',
        }),
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
    expect(await screen.findByText(/Completed in 3 ms/, {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText(/Truncated/)).not.toBeInTheDocument();
  });

  it('polls a running invocation and shows its error once it failed', async () => {
    let polls = 0;
    const fetchMock = stubInt({
      ...route({ status: 'active' }),
      'POST /api/v1/orgs/acme/connections/c-1/invoke': () =>
        jsonResponse(INVOCATION, { status: 202 }),
      [INVOCATION_URL]: () => {
        polls += 1;
        return jsonResponse(
          polls === 1
            ? { ...INVOCATION, status: 'running' }
            : {
                ...INVOCATION,
                status: 'failed',
                error: 'egress_denied',
                finished_at: '2026-11-22T10:00:02Z',
              },
        );
      },
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.type(await screen.findByLabelText('Path'), '/orders');
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(
      await screen.findByText('Invocation running...', {}, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/The invocation failed \(egress_denied\)/, {}, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText('Invocation running...')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Invocation result' })).not.toBeInTheDocument();
    expect(fetchMock.calls.filter((c) => c.key === INVOCATION_URL)).toHaveLength(2);
  }, 10_000);

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

const DBX_ADAPTER = {
  key: 'databricks',
  kind: 'connection',
  version: '1.0.0',
  capabilities: [{ capability: 'query.sql', maturity: 'experimental' }],
  config_schema: {
    type: 'object',
    required: ['host'],
    properties: {
      host: { type: 'string', title: 'Workspace URL' },
      client_id: { type: 'string', title: 'OAuth client id' },
      max_rows: { type: 'integer', minimum: 1, maximum: 10000, title: 'Maximum rows' },
    },
  },
  secret_fields: ['token', 'client_secret'],
  auth_methods: ['none', 'bearer', 'oauth2_client_credentials'],
  auth_only_fields: { client_id: ['oauth2_client_credentials'] },
  destination_fields: ['host'],
};
const DBX = {
  connection_type: 'data_platform',
  adapter_key: 'databricks',
  config: { host: 'https://adb-1.7.azuredatabricks.net', max_rows: 100 },
};
const CREDENTIAL_URL = 'PUT /api/v1/orgs/acme/connections/c-1/credential';
const NO_SECRET_MANAGE = [...ALL_PERMS, ...INT_PERMS].filter((p) => p !== 'secret:manage');

describe('ConnectionDetailView: edit, credential, archive and discovery (CM-7)', () => {
  it('shows whether a credential is set', async () => {
    stubInt(route({ secret_set: true }));
    renderWithClient(<ConnectionDetailView id="c-1" />);
    const item = (await screen.findByText('Credential', { selector: 'dt' })).parentElement!;
    expect(item).toHaveTextContent('Set');
  });

  it('rotates with an empty password input, row_version and read_only_confirmed, then tests now', async () => {
    const fetchMock = stubInt({
      ...route({ secret_set: true }),
      [CREDENTIAL_URL]: () => jsonResponse({ ...BASE, secret_set: true, row_version: 6 }),
      'POST /api/v1/orgs/acme/connections/c-1/test': () => jsonResponse(QUEUED, { status: 202 }),
      'GET /api/v1/orgs/acme/connections/c-1/test-jobs/j-1': () =>
        jsonResponse({ ...QUEUED, status: 'done', result: PASS }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Rotate credential' }));
    const dialog = screen.getByRole('dialog');
    const input = within(dialog).getByLabelText('New credential value');
    expect(input).toHaveValue('');
    expect(input).toHaveAttribute('type', 'password');
    expect(within(dialog).getByText(/Other connections that bind this secret/)).toBeInTheDocument();
    await userEvent.type(input, 'new-token');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Rotate credential' }));
    expect(
      within(dialog).getByText('Confirm that the credential is read-only.'),
    ).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === CREDENTIAL_URL)).toBe(false);
    await userEvent.click(within(dialog).getByLabelText('I confirm this credential is read-only'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Rotate credential' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, CREDENTIAL_URL)).toEqual({
        value: 'new-token',
        row_version: 5,
        read_only_confirmed: true,
      }),
    );
    await userEvent.click(await screen.findByRole('button', { name: 'Test now' }));
    await waitFor(() =>
      expect(
        fetchMock.calls.some((c) => c.key === 'POST /api/v1/orgs/acme/connections/c-1/test'),
      ).toBe(true),
    );
    expect(
      await screen.findByRole('table', { name: 'Checks for this test run' }, { timeout: 5000 }),
    ).toBeInTheDocument();
  });

  it('clears the dialog value on cancel', async () => {
    stubInt(route({ secret_set: true }));
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Rotate credential' }));
    await userEvent.type(screen.getByLabelText('New credential value'), 'typed');
    await userEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Rotate credential' }));
    expect(screen.getByLabelText('New credential value')).toHaveValue('');
  });

  it('hides Set/Rotate without secret:manage or for auth method none; reads Set credential when none is set', async () => {
    stubInt(route({ secret_set: true }), NO_SECRET_MANAGE);
    const first = renderWithClient(<ConnectionDetailView id="c-1" />);
    await screen.findByText('https://orders.example.test');
    expect(screen.queryByRole('button', { name: 'Rotate credential' })).not.toBeInTheDocument();
    first.unmount();
    vi.unstubAllGlobals();

    stubInt(route({ auth_method: 'none', secret_id: null, secret_set: false }));
    const second = renderWithClient(<ConnectionDetailView id="c-1" />);
    await screen.findByText('https://orders.example.test');
    expect(screen.queryByRole('button', { name: /credential/ })).not.toBeInTheDocument();
    second.unmount();
    vi.unstubAllGlobals();

    stubInt(route({ secret_set: false }));
    renderWithClient(<ConnectionDetailView id="c-1" />);
    expect(await screen.findByRole('button', { name: 'Set credential' })).toBeInTheDocument();
  });

  it('edits the host with a destination warning and sends only config and row_version', async () => {
    const fetchMock = stubInt({
      ...route(DBX),
      'GET /api/v1/adapters': () => list([DBX_ADAPTER]),
      'PATCH /api/v1/orgs/acme/connections/c-1': () => jsonResponse({ ...BASE, ...DBX }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const dialog = screen.getByRole('dialog');
    const host = within(dialog).getByLabelText('Workspace URL (required)');
    expect(host).toHaveValue('https://adb-1.7.azuredatabricks.net');
    expect(within(dialog).queryByLabelText('OAuth client id')).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/sent to the new destination/)).not.toBeInTheDocument();
    await userEvent.clear(host);
    await userEvent.type(host, 'https://adb-2.7.azuredatabricks.net');
    expect(within(dialog).getByText(/sent to the new destination/)).toHaveTextContent(
      /secret:use .* returns to draft/,
    );
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'PATCH /api/v1/orgs/acme/connections/c-1')).toEqual({
        config: { host: 'https://adb-2.7.azuredatabricks.net', max_rows: 100 },
        row_version: 5,
      }),
    );
  });

  it('edits only the name without a warning', async () => {
    const fetchMock = stubInt({
      ...route({}),
      'PATCH /api/v1/orgs/acme/connections/c-1': () => jsonResponse({ ...BASE, name: 'Orders v2' }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.clear(within(dialog).getByLabelText('Name'));
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Orders v2');
    expect(within(dialog).queryByText(/sent to the new destination/)).not.toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'PATCH /api/v1/orgs/acme/connections/c-1')).toEqual({
        name: 'Orders v2',
        row_version: 5,
      }),
    );
  });

  it('shows a stale edit refusal', async () => {
    stubInt({
      ...route({}),
      'PATCH /api/v1/orgs/acme/connections/c-1': () =>
        problemResponse(409, 'stale_row_version', { detail: 'The connection changed.' }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), ' 2');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    expect(await within(dialog).findByText('The connection changed.')).toBeInTheDocument();
  });

  it('hides Edit for a retired connection', async () => {
    stubInt(route({ status: 'retired' }));
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await screen.findByText('https://orders.example.test');
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Rotate credential' })).not.toBeInTheDocument();
  });

  it('says in the retire dialog that the own credential is disabled', async () => {
    stubInt(route({ status: 'active' }));
    renderWithClient(<ConnectionDetailView id="c-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retire' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/Retiring archives the connection/)).toHaveTextContent(
      /own credential is disabled unless another live connection uses it/,
    );
    expect(within(dialog).getByText(/A shared secret it uses is kept/)).toBeInTheDocument();
  });

  it('renders discovered tables as catalog > schema > table with a truncation notice', async () => {
    stubInt({
      ...route(DBX),
      'GET /api/v1/adapters': () => list([DBX_ADAPTER]),
      'GET /api/v1/orgs/acme/connections/c-1/discoveries/latest': () =>
        jsonResponse({
          discovered_at: '2026-11-22T10:00:00Z',
          payload: {
            tables: [
              {
                catalog: 'main',
                schema: 'sales',
                name: 'orders',
                table_type: 'MANAGED',
                columns: [{ name: 'order_id', type: 'bigint', nullable: false }],
              },
              {
                catalog: 'samples',
                schema: 'nyctaxi',
                name: 'trips',
                table_type: 'MANAGED',
                columns: [{ name: 'fare', type: 'double', nullable: true }],
              },
            ],
            truncated: true,
          },
        }),
    });
    renderWithClient(<ConnectionDetailView id="c-1" />);
    const catalogs = await screen.findByRole('list', { name: 'Catalogs' });
    expect(within(catalogs).getByText('main')).toBeInTheDocument();
    expect(within(catalogs).getByText('samples')).toBeInTheDocument();
    const schemas = within(catalogs).getByRole('list', { name: 'Schemas in main' });
    expect(within(schemas).getByText('sales')).toBeInTheDocument();
    const tables = within(schemas).getByRole('list', { name: 'Tables in main.sales' });
    expect(within(tables).getByText('orders')).toBeInTheDocument();
    expect(within(tables).getByText(/order_id bigint/)).toBeInTheDocument();
    expect(
      within(within(catalogs).getByRole('list', { name: 'Tables in samples.nyctaxi' })).getByText(
        'trips',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/hit its limits/)).toHaveTextContent(
      'not every catalog, schema or table is shown',
    );
  });
});
