import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Adapter } from '@/lib/api/integrate-types';
import { ADAPTER_REST, INT_PERMS, stubInt } from '@/test/integrate';
import { ALL_PERMS, bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { ConnectionWizard } from './ConnectionWizard';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const CONN = { id: 'c-1', name: 'Orders API', status: 'draft', row_version: 1 };
const base = {
  'GET /api/v1/orgs/acme/vendors': () => list([]),
  'GET /api/v1/orgs/acme/secrets': () =>
    list([
      {
        id: 'sec-1',
        name: 'orders-token',
        version: 3,
        status: 'active',
        scope: { type: 'workspace', id: 'ws-1' },
      },
      {
        id: 'sec-old',
        name: 'old-token',
        version: 1,
        status: 'disabled',
        scope: { type: 'workspace', id: 'ws-1' },
      },
    ]),
  'GET /api/v1/orgs/acme/egress-policies': () =>
    list([{ id: 'egr-1', name: 'Corp only', row_version: 1 }]),
  'GET /api/v1/orgs/acme/workspaces': () =>
    list([{ id: 'ws-1', slug: 'eng', name: 'Engineering', row_version: 1 }]),
  'GET /api/v1/orgs/acme/workspaces/eng/projects': () => list([]),
};

type User = ReturnType<typeof userEvent.setup>;

/** Kind (rest.http, optionally another auth method) and Scope (workspace eng), up to Configuration. */
async function fillBasics(user: User, auth?: string) {
  await user.type(screen.getByLabelText('Name'), 'Orders API');
  await screen.findByRole('option', { name: 'rest.http' });
  await user.selectOptions(screen.getByLabelText('Adapter'), 'rest.http');
  if (auth) await user.selectOptions(screen.getByLabelText('Authentication method'), auth);
  await user.click(screen.getByRole('button', { name: 'Next' }));
  await screen.findByRole('option', { name: 'Engineering' });
  await user.selectOptions(screen.getByLabelText('Workspace'), 'eng');
  await user.click(screen.getByRole('button', { name: 'Next' }));
}

const next = (user: User) => user.click(screen.getByRole('button', { name: 'Next' }));

describe('ConnectionWizard', () => {
  it('renders config fields from the adapter config_schema, excluding secret fields', async () => {
    const user = userEvent.setup();
    stubInt(base);
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    await fillBasics(user);
    expect(screen.getByLabelText('Base URL (required)')).toBeInTheDocument();
    expect(screen.getByLabelText('Timeout seconds')).toHaveAttribute('type', 'number');
    expect(screen.getByLabelText('Verify TLS')).toBeChecked();
    expect(screen.getByLabelText('Allowed methods').tagName).toBe('TEXTAREA');
    expect(screen.getByLabelText('Auth style').tagName).toBe('SELECT');
    expect(screen.queryByLabelText(/API token/)).not.toBeInTheDocument();
  });

  it('validates required and bounded fields on the client before advancing', async () => {
    const user = userEvent.setup();
    stubInt(base);
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    await fillBasics(user);
    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByText('Base URL is required.')).toBeInTheDocument();
    expect(screen.getByLabelText('Base URL (required)')).toHaveAttribute('aria-invalid', 'true');
    await user.type(screen.getByLabelText('Base URL (required)'), 'https://orders.example.test');
    await user.type(screen.getByLabelText('Timeout seconds'), '99');
    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByText('Must be at most 60.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Authentication method')).not.toBeInTheDocument();
  });

  it('creates with secret_id, egress_policy_id and scope in the payload; never lists disabled secrets', async () => {
    const user = userEvent.setup();
    const created = vi.fn();
    const fetchMock = stubInt({
      ...base,
      'POST /api/v1/orgs/acme/connections': () => jsonResponse(CONN, { status: 201 }),
    });
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={created} />);
    await fillBasics(user, 'bearer');
    await user.type(screen.getByLabelText('Base URL (required)'), 'https://orders.example.test');
    await user.type(screen.getByLabelText('Timeout seconds'), '30');
    await user.type(screen.getByLabelText('Allowed methods'), 'GET{Enter}POST');
    await user.click(screen.getByRole('button', { name: 'Next' }));

    await user.click(await screen.findByLabelText('Use an existing secret of this scope'));
    expect(screen.queryByRole('option', { name: /old-token/ })).not.toBeInTheDocument();
    await next(user);
    expect(screen.getByText(/Choose the secret/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Secret'), 'sec-1');
    await next(user);
    await user.selectOptions(screen.getByLabelText('Egress policy'), 'egr-1');
    await user.click(screen.getByLabelText('I confirm this credential is read-only'));
    await next(user);
    await user.click(screen.getByRole('button', { name: 'Create connection' }));

    await waitFor(() => expect(created).toHaveBeenCalledWith('c-1'));
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/connections')).toEqual({
      name: 'Orders API',
      scope: { workspace: 'eng' },
      connection_type: 'rest',
      adapter_key: 'rest.http',
      config: {
        base_url: 'https://orders.example.test',
        timeout_seconds: 30,
        verify_tls: true,
        allowed_methods: ['GET', 'POST'],
      },
      auth_method: 'bearer',
      secret_id: 'sec-1',
      egress_policy_id: 'egr-1',
      access_mode: 'read',
      read_only_confirmed: true,
    });
  });

  it.each([
    ['health_path', "path must not contain '.' or '..' segments"],
    ['base_url', 'must not contain a query string or a fragment'],
  ])('shows the server text for a rejected %s', async (field, msg) => {
    const user = userEvent.setup();
    stubInt({
      ...base,
      'POST /api/v1/orgs/acme/connections': () =>
        problemResponse(422, 'validation_error', {
          errors: [{ loc: ['body', 'config', field], msg }],
        }),
    });
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    await fillBasics(user);
    await user.type(screen.getByLabelText('Base URL (required)'), 'https://orders.example.test');
    await user.click(screen.getByRole('button', { name: 'Next' }));
    await next(user);
    await user.click(screen.getByRole('button', { name: 'Create connection' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(msg);
  });

  it('omits secret_id for auth method none and offers no secret picker', async () => {
    const user = userEvent.setup();
    const fetchMock = stubInt({
      ...base,
      'POST /api/v1/orgs/acme/connections': () => jsonResponse(CONN, { status: 201 }),
    });
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    await fillBasics(user);
    await user.type(screen.getByLabelText('Base URL (required)'), 'https://orders.example.test');
    await user.click(screen.getByRole('button', { name: 'Next' }));
    // Access, back over the skipped Credential step, Configuration and Scope to Kind
    for (let i = 0; i < 3; i += 1) await user.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByLabelText('Authentication method')).toHaveValue('none');
    expect(screen.queryByLabelText('Secret')).not.toBeInTheDocument();
    for (let i = 0; i < 4; i += 1) await next(user);
    await user.click(screen.getByRole('button', { name: 'Create connection' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/connections')).toMatchObject({
        auth_method: 'none',
      }),
    );
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/connections')).not.toHaveProperty('secret_id');
  });

  it('maps server field errors back onto the configuration field', async () => {
    const user = userEvent.setup();
    stubInt({
      ...base,
      'POST /api/v1/orgs/acme/connections': () =>
        problemResponse(422, 'validation_error', {
          errors: [{ loc: ['body', 'config', 'base_url'], msg: 'host is not allowed' }],
        }),
    });
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    await fillBasics(user);
    await user.type(screen.getByLabelText('Base URL (required)'), 'https://orders.example.test');
    await user.click(screen.getByRole('button', { name: 'Next' }));
    await next(user);
    await user.click(screen.getByRole('button', { name: 'Create connection' }));
    await user.click(await screen.findByRole('button', { name: 'Go to the field with the error' }));
    const field = screen.getByLabelText('Base URL (required)');
    expect(field).toHaveAttribute('aria-invalid', 'true');
    expect(within(field.parentElement!).getByText('host is not allowed')).toBeInTheDocument();
  });

  it('marks planned adapters as declared only and not selectable for use', async () => {
    const user = userEvent.setup();
    stubInt(base);
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    const option = await screen.findByRole('option', { name: /databricks \(declared only/ });
    await user.selectOptions(screen.getByLabelText('Adapter'), option);
    expect(screen.getByText(/cannot be tested or invoked/)).toBeInTheDocument();
  });
});

const ADAPTER_DATABRICKS: Adapter = {
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
      token: { type: 'string', title: 'Token' },
    },
  },
  secret_fields: ['token', 'client_secret'],
  auth_methods: ['none', 'bearer', 'oauth2_client_credentials'],
};
const HOST = 'https://adb-1.7.azuredatabricks.net';
const TOKEN = 'dapi-very-secret-value';
const SCOPED_SECRETS = [
  {
    id: 's-t',
    name: 'tenant-token',
    version: 1,
    status: 'active',
    scope: { type: 'tenant', id: 'org-1' },
  },
  {
    id: 's-w',
    name: 'ws-token',
    version: 1,
    status: 'active',
    scope: { type: 'workspace', id: 'ws-1' },
  },
  {
    id: 's-p',
    name: 'proj-token',
    version: 1,
    status: 'active',
    scope: { type: 'project', id: 'p-1' },
  },
  {
    id: 's-c',
    name: 'connection-c-9',
    version: 1,
    status: 'active',
    scope: { type: 'workspace', id: 'ws-1' },
  },
  {
    id: 's-d',
    name: 'off-token',
    version: 1,
    status: 'disabled',
    scope: { type: 'workspace', id: 'ws-1' },
  },
];

/** Kind for Databricks with `auth`, then Scope (workspace eng when `ws`), up to Configuration. */
async function databricksKind(user: User, auth: string, ws = true) {
  await user.type(screen.getByLabelText('Name'), 'Lakehouse');
  await screen.findByRole('option', { name: 'databricks' });
  await user.selectOptions(screen.getByLabelText('Adapter'), 'databricks');
  await user.selectOptions(screen.getByLabelText('Connection type'), 'data_platform');
  await user.selectOptions(screen.getByLabelText('Authentication method'), auth);
  await next(user);
  if (ws) {
    await screen.findByRole('option', { name: 'Engineering' });
    await user.selectOptions(screen.getByLabelText('Workspace'), 'eng');
  }
  await next(user);
}

describe('ConnectionWizard: the uniform flow (CM-7)', () => {
  const stubDbx = (routes: Parameters<typeof stubInt>[0] = {}, perms?: string[]) =>
    stubInt({ ...base, ...routes }, perms, [ADAPTER_REST, ADAPTER_DATABRICKS]);

  it('lists the steps Kind, Scope, Configuration, Credential, Access, Review in order', async () => {
    stubDbx();
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    const steps = within(await screen.findByRole('list', { name: 'Steps' })).getAllByRole(
      'listitem',
    );
    expect(steps.map((s) => s.textContent)).toEqual([
      '1. Kind',
      '2. Scope',
      '3. Configuration',
      '4. Credential',
      '5. Access',
      '6. Review',
    ]);
  });

  it('shows an auth-only field only under its auth method', async () => {
    const user = userEvent.setup();
    stubDbx();
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    await databricksKind(user, 'bearer', false);
    expect(screen.getByLabelText('Workspace URL (required)')).toBeInTheDocument();
    expect(screen.queryByLabelText('OAuth client id')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Token')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Back' }));
    await user.click(screen.getByRole('button', { name: 'Back' }));
    await user.selectOptions(
      screen.getByLabelText('Authentication method'),
      'oauth2_client_credentials',
    );
    await next(user);
    await next(user);
    expect(screen.getByLabelText('OAuth client id')).toBeInTheDocument();
  });

  it('sends an inline credential as credential.value, gated on the read-only confirmation, never shown in review', async () => {
    const user = userEvent.setup();
    const created = vi.fn();
    const fetchMock = stubDbx({
      'POST /api/v1/orgs/acme/connections': () => jsonResponse(CONN, { status: 201 }),
    });
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={created} />);
    await databricksKind(user, 'bearer');
    await user.type(screen.getByLabelText('Workspace URL (required)'), HOST);
    await next(user);

    const input = screen.getByLabelText('Credential value');
    expect(input).toHaveAttribute('type', 'password');
    await next(user);
    expect(screen.getByText('Enter the credential value.')).toBeInTheDocument();
    await user.type(input, TOKEN);
    await next(user);

    const box = screen.getByLabelText('I confirm this credential is read-only');
    expect(screen.getByText(/the read-only credential is the real control/i)).toBeInTheDocument();
    await next(user);
    expect(screen.getByText('Confirm that the credential is read-only.')).toBeInTheDocument();
    expect(screen.getByLabelText('Access mode')).toBeInTheDocument();
    await user.click(box);
    await next(user);

    const review = screen.getByRole('list', { name: 'Steps' }).parentElement!;
    expect(screen.getByText('Set (hidden)')).toBeInTheDocument();
    expect(review.textContent).not.toContain(TOKEN);
    await user.click(screen.getByRole('button', { name: 'Create connection' }));
    await waitFor(() => expect(created).toHaveBeenCalledWith('c-1'));
    const body = bodyOf(fetchMock, 'POST /api/v1/orgs/acme/connections');
    expect(body).toMatchObject({
      adapter_key: 'databricks',
      auth_method: 'bearer',
      config: { host: HOST },
      credential: { value: TOKEN },
      access_mode: 'read',
      read_only_confirmed: true,
    });
    expect(body).not.toHaveProperty('secret_id');
  });

  it('lists only active, non-connection secrets of the chosen scope', async () => {
    const user = userEvent.setup();
    stubDbx({ 'GET /api/v1/orgs/acme/secrets': () => list(SCOPED_SECRETS) });
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    await databricksKind(user, 'bearer');
    await user.type(screen.getByLabelText('Workspace URL (required)'), HOST);
    await next(user);
    await user.click(await screen.findByLabelText('Use an existing secret of this scope'));
    const options = within(screen.getByLabelText('Secret'))
      .getAllByRole('option')
      .map((o) => o.textContent);
    expect(options).toEqual(['Select a secret...', 'ws-token (version 1)']);
    expect(screen.queryByLabelText('Credential value')).not.toBeInTheDocument();
  });

  it('offers no existing-secret picker without secret:manage at the chosen scope', async () => {
    const user = userEvent.setup();
    const scoped = [...ALL_PERMS, ...INT_PERMS].filter((p) => p !== 'secret:manage');
    stubDbx({
      'GET /api/v1/orgs/acme/secrets': () => list(SCOPED_SECRETS),
      'GET /api/v1/orgs/acme/me/permissions': ({ url }) =>
        jsonResponse({
          permissions: url.includes('workspace=eng') ? scoped : [...ALL_PERMS, ...INT_PERMS],
        }),
    });
    renderWithClient(<ConnectionWizard org="acme" onClose={() => {}} onCreated={() => {}} />);
    await databricksKind(user, 'bearer');
    await user.type(screen.getByLabelText('Workspace URL (required)'), HOST);
    await next(user);
    expect(screen.getByLabelText('Credential value')).toHaveAttribute('type', 'password');
    expect(
      screen.getByText(/needs the secret:manage permission at this scope/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText('Use an existing secret of this scope')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Secret')).not.toBeInTheDocument();
  });
});
