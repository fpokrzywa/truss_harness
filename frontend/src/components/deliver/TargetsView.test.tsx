import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { D, DELIVERY_READ_PERMS, list, stubDelivery, TARGET } from '@/test/delivery';
import { bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { TargetsView } from './TargetsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const CLAIMED = 'This platform address is already registered as a deployment target.';
const WORKSPACES = {
  'GET /api/v1/orgs/acme/workspaces': () =>
    list([{ id: 'ws-1', slug: 'eng', name: 'Engineering', status: 'active', row_version: 1 }]),
  'GET /api/v1/orgs/acme/workspaces/eng/projects': () =>
    list([{ id: 'p-1', slug: 'api', name: 'API', status: 'active', row_version: 1 }]),
};

async function fillRegister() {
  await userEvent.click(await screen.findByRole('button', { name: 'Register target' }));
  const dialog = screen.getByRole('dialog');
  await userEvent.type(within(dialog).getByLabelText('Name'), 'Analytics prod');
  await waitFor(() =>
    expect(within(dialog).getByRole('option', { name: 'Engineering' })).toBeInTheDocument(),
  );
  await userEvent.selectOptions(within(dialog).getByLabelText('Workspace'), 'eng');
  await waitFor(() =>
    expect(within(dialog).getByRole('option', { name: 'API' })).toBeInTheDocument(),
  );
  await userEvent.selectOptions(within(dialog).getByLabelText('Project (optional)'), 'api');
  await userEvent.type(
    within(dialog).getByLabelText('Address'),
    'https://adb-123.azuredatabricks.net',
  );
  await userEvent.selectOptions(within(dialog).getByLabelText('Environment'), 'prod');
  await userEvent.type(within(dialog).getByLabelText('Warehouse ID'), 'wh-0042');
  await userEvent.type(within(dialog).getByLabelText('Credential reference'), 'dbx.prod');
  return dialog;
}

describe('TargetsView list', () => {
  it('renders the targets with an Experimental badge on a Databricks target, never Validated', async () => {
    stubDelivery();
    renderWithClient(<TargetsView />);
    const table = await screen.findByRole('table', { name: 'Deployment targets' });
    const row = within(table).getByRole('cell', { name: 'Analytics prod' }).closest('tr')!;
    expect(within(row).getByText('Databricks')).toBeInTheDocument();
    expect(within(row).getByText('Experimental')).toBeInTheDocument();
    expect(within(row).getByText('https://adb-123.azuredatabricks.net')).toBeInTheDocument();
    expect(within(row).getByText('prod')).toBeInTheDocument();
    expect(within(row).getByText(/eng/)).toHaveTextContent('eng / api');
    expect(within(row).getByText('wh-0042')).toBeInTheDocument();
    expect(within(row).getByText('dbx.prod')).toBeInTheDocument();
    expect(within(row).getByText('active')).toBeInTheDocument();
    expect(screen.queryByText('Validated')).not.toBeInTheDocument();
  });

  it('shows the serving endpoint in its column, and - when the target has none', async () => {
    stubDelivery({
      [`GET ${D}/targets`]: () =>
        list([
          {
            ...TARGET,
            bindings: { warehouse_id: 'wh-0042', serving_endpoint: 'llama-serving' },
          },
          { ...TARGET, id: 't-2', name: 'Analytics dev' },
        ]),
    });
    renderWithClient(<TargetsView />);
    const table = await screen.findByRole('table', { name: 'Deployment targets' });
    expect(
      within(table).getByRole('columnheader', { name: 'Serving endpoint' }),
    ).toBeInTheDocument();
    const withEndpoint = within(table).getByRole('cell', { name: 'Analytics prod' }).closest('tr')!;
    expect(within(withEndpoint).getByRole('cell', { name: 'llama-serving' })).toBeInTheDocument();
    const without = within(table).getByRole('cell', { name: 'Analytics dev' }).closest('tr')!;
    const cells = within(without).getAllByRole('cell');
    const headers = within(table).getAllByRole('columnheader');
    const column = headers.findIndex((h) => h.textContent === 'Serving endpoint');
    expect(cells[column]).toHaveTextContent(/^-$/);
  });

  it('shows an empty state', async () => {
    stubDelivery({ [`GET ${D}/targets`]: () => list([]) });
    renderWithClient(<TargetsView />);
    expect(await screen.findByText(/No deployment targets yet/)).toBeInTheDocument();
  });
});

describe('TargetsView register', () => {
  it('validates required fields before calling the API', async () => {
    const fetchMock = stubDelivery();
    renderWithClient(<TargetsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Register target' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Register target' }));
    expect(await within(dialog).findByText('Name is required.')).toBeInTheDocument();
    expect(within(dialog).getByText('Choose a workspace.')).toBeInTheDocument();
    expect(within(dialog).getByText('Address is required.')).toBeInTheDocument();
    expect(within(dialog).getByText('Warehouse ID is required.')).toBeInTheDocument();
    expect(within(dialog).getByText('Credential reference is required.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('posts the register body, closes the dialog and refreshes the list', async () => {
    const fetchMock = stubDelivery({
      ...WORKSPACES,
      [`POST ${D}/targets`]: () => jsonResponse(TARGET, { status: 201 }),
    });
    renderWithClient(<TargetsView />);
    const dialog = await fillRegister();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Register target' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/targets`)).toEqual({
      workspace: 'eng',
      project: 'api',
      name: 'Analytics prod',
      platform_kind: 'databricks',
      address: 'https://adb-123.azuredatabricks.net',
      environment: 'prod',
      bindings: { warehouse_id: 'wh-0042' },
      credential_ref: 'dbx.prod',
    });
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `GET ${D}/targets`).length).toBe(2),
    );
  });

  it('omits serving_endpoint from the bindings when the field is left empty', async () => {
    const fetchMock = stubDelivery({
      ...WORKSPACES,
      [`POST ${D}/targets`]: () => jsonResponse(TARGET, { status: 201 }),
    });
    renderWithClient(<TargetsView />);
    const dialog = await fillRegister();
    expect(within(dialog).getByLabelText('Serving endpoint (optional)')).toHaveValue('');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Register target' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const body = bodyOf(fetchMock, `POST ${D}/targets`) as { bindings: Record<string, unknown> };
    expect(body.bindings).toEqual({ warehouse_id: 'wh-0042' });
    expect('serving_endpoint' in body.bindings).toBe(false);
  });

  it('posts a trimmed serving endpoint, closes the dialog and shows it in the refreshed list', async () => {
    const saved = {
      ...TARGET,
      bindings: { warehouse_id: 'wh-0042', serving_endpoint: 'llama_serving-01' },
    };
    let registered = false;
    const fetchMock = stubDelivery({
      ...WORKSPACES,
      [`GET ${D}/targets`]: () => list(registered ? [saved] : []),
      [`POST ${D}/targets`]: () => {
        registered = true;
        return jsonResponse(saved, { status: 201 });
      },
    });
    renderWithClient(<TargetsView />);
    const dialog = await fillRegister();
    await userEvent.type(
      within(dialog).getByLabelText('Serving endpoint (optional)'),
      '  llama_serving-01  ',
    );
    await userEvent.click(within(dialog).getByRole('button', { name: 'Register target' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect((bodyOf(fetchMock, `POST ${D}/targets`) as { bindings: unknown }).bindings).toEqual({
      warehouse_id: 'wh-0042',
      serving_endpoint: 'llama_serving-01',
    });
    const table = await screen.findByRole('table', { name: 'Deployment targets' });
    const row = within(table).getByRole('cell', { name: 'Analytics prod' }).closest('tr')!;
    expect(within(row).getByRole('cell', { name: 'llama_serving-01' })).toBeInTheDocument();
  });

  it.each(['bad.name', 'has space'])(
    'refuses the serving endpoint %j before calling the API',
    async (value) => {
      const fetchMock = stubDelivery(WORKSPACES);
      renderWithClient(<TargetsView />);
      const dialog = await fillRegister();
      await userEvent.type(within(dialog).getByLabelText('Serving endpoint (optional)'), value);
      await userEvent.click(within(dialog).getByRole('button', { name: 'Register target' }));
      expect(
        await within(dialog).findByText('Use 1-63 letters, digits, underscores or hyphens.'),
      ).toBeInTheDocument();
      expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
    },
  );

  it('shows the 409 platform_already_registered detail verbatim inside the dialog', async () => {
    stubDelivery({
      ...WORKSPACES,
      [`POST ${D}/targets`]: () =>
        problemResponse(409, 'platform_already_registered', { detail: CLAIMED }),
    });
    renderWithClient(<TargetsView />);
    const dialog = await fillRegister();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Register target' }));
    const alert = await within(dialog).findByText(CLAIMED);
    expect(alert.closest('[role="alert"]')).toHaveTextContent('Could not register the target');
    expect(within(dialog).getByText('platform_already_registered')).toBeInTheDocument();
  });
});

describe('TargetsView archive', () => {
  it('archives only after confirmation, then refreshes', async () => {
    const fetchMock = stubDelivery({
      [`POST ${D}/targets/t-1/archive`]: () => jsonResponse({ ...TARGET, status: 'archived' }),
    });
    renderWithClient(<TargetsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Archive/ }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Archive target?');
    expect(fetchMock.calls.some((c) => c.key.endsWith('/archive'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Archive target' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${D}/targets/t-1/archive`)).toBe(true),
    );
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `GET ${D}/targets`).length).toBe(2),
    );
  });

  it('shows an archive error instead of swallowing it', async () => {
    stubDelivery({
      [`POST ${D}/targets/t-1/archive`]: () =>
        problemResponse(409, 'conflict', { detail: 'the target is already archived' }),
    });
    renderWithClient(<TargetsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Archive/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Archive target' }));
    expect(await screen.findByText('the target is already archived')).toBeInTheDocument();
    expect(screen.getByText('Could not archive the target')).toBeInTheDocument();
  });

  it('has no Register or Archive buttons without delivery:target:write', async () => {
    stubDelivery({}, DELIVERY_READ_PERMS);
    renderWithClient(<TargetsView />);
    expect(await screen.findByText('Analytics prod')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Register target' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Archive/ })).not.toBeInTheDocument();
  });
});

describe('TargetsView plain text', () => {
  it('renders markup and URLs in server values as literal text', async () => {
    const evil = '<img src=x onerror=alert(1)> https://evil.example/';
    stubDelivery({
      [`GET ${D}/targets`]: () =>
        list([{ ...TARGET, name: evil, address: 'https://evil.example/', credential_ref: evil }]),
    });
    const { container } = renderWithClient(<TargetsView />);
    const row = (await screen.findAllByText(evil))[0]!.closest('tr')!;
    const cells = within(row)
      .getAllByRole('cell')
      .filter((c) => c.textContent === evil);
    expect(cells).toHaveLength(2); // name and credential reference, as text
    expect(within(row).getByText('https://evil.example/')).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a[href="https://evil.example/"]')).toBeNull();
    expect(container.querySelectorAll('a')).toHaveLength(0);
  });
});
