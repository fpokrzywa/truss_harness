import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { stubInt } from '@/test/integrate';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { ConnectionWizard } from './ConnectionWizard';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const CONN = { id: 'c-1', name: 'Orders API', status: 'draft', row_version: 1 };
const base = {
  'GET /api/v1/orgs/acme/vendors': () => list([]),
  'GET /api/v1/orgs/acme/secrets': () =>
    list([
      { id: 'sec-1', name: 'orders-token', version: 3, status: 'active' },
      { id: 'sec-old', name: 'old-token', version: 1, status: 'disabled' },
    ]),
  'GET /api/v1/orgs/acme/egress-policies': () =>
    list([{ id: 'egr-1', name: 'Corp only', row_version: 1 }]),
  'GET /api/v1/orgs/acme/workspaces': () =>
    list([{ id: 'ws-1', slug: 'eng', name: 'Engineering', row_version: 1 }]),
  'GET /api/v1/orgs/acme/workspaces/eng/projects': () => list([]),
};

async function fillBasics(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Name'), 'Orders API');
  await screen.findByRole('option', { name: 'rest.http' });
  await user.selectOptions(screen.getByLabelText('Adapter'), 'rest.http');
  await screen.findByRole('option', { name: 'Engineering' });
  await user.selectOptions(screen.getByLabelText('Workspace'), 'eng');
  await user.click(screen.getByRole('button', { name: 'Next' }));
}

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
    await fillBasics(user);
    await user.type(screen.getByLabelText('Base URL (required)'), 'https://orders.example.test');
    await user.type(screen.getByLabelText('Timeout seconds'), '30');
    await user.type(screen.getByLabelText('Allowed methods'), 'GET{Enter}POST');
    await user.click(screen.getByRole('button', { name: 'Next' }));

    await user.selectOptions(screen.getByLabelText('Authentication method'), 'bearer');
    expect(screen.queryByRole('option', { name: /old-token/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Create connection' }));
    expect(screen.getByText(/Choose the secret/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Secret'), 'sec-1');
    await user.selectOptions(screen.getByLabelText('Egress policy'), 'egr-1');
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
    expect(screen.getByLabelText('Authentication method')).toHaveValue('none');
    expect(screen.queryByLabelText('Secret')).not.toBeInTheDocument();
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
