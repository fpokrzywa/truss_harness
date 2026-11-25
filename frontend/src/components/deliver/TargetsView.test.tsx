import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  D,
  DELIVERY_READ_PERMS,
  list,
  SNOWFLAKE_TARGET,
  stubDelivery,
  TARGET,
} from '@/test/delivery';
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

  it('shows the bindings of each platform in their columns, and - for the other platform', async () => {
    stubDelivery({ [`GET ${D}/targets`]: () => list([TARGET, SNOWFLAKE_TARGET]) });
    renderWithClient(<TargetsView />);
    const table = await screen.findByRole('table', { name: 'Deployment targets' });
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((h) => h.textContent);
    const cellsOf = (name: string) => {
      const row = within(table).getByRole('cell', { name }).closest('tr')!;
      const cells = within(row).getAllByRole('cell');
      return Object.fromEntries(headers.map((h, i) => [h, cells[i]?.textContent]));
    };
    expect(cellsOf('Finance prod')).toMatchObject({
      Kind: 'SnowflakeExperimental',
      Address: 'https://acme-fin.snowflakecomputing.com',
      Warehouse: 'APP_WH',
      'Serving endpoint': '-',
      Package: 'SALES_PKG',
      Application: 'SALES_APP',
      'Cortex model': 'llama3.1-70b',
      'Credential reference': 'sf.prod',
    });
    expect(cellsOf('Analytics prod')).toMatchObject({
      Kind: 'DatabricksExperimental',
      Warehouse: 'wh-0042',
      'Serving endpoint': '-',
      Package: '-',
      Application: '-',
      'Cortex model': '-',
      'Credential reference': 'dbx.prod',
    });
    expect(headers).not.toContain('Warehouse ID');
    expect(screen.queryByText('Validated')).not.toBeInTheDocument();
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

describe('TargetsView register on Snowflake', () => {
  const IDENTIFIER_RULE = 'Use 1-255 letters, digits, _ or $, starting with a letter or _.';
  const CORTEX_MODEL_RULE =
    'Use 1-64 lowercase letters, digits, dots, underscores or hyphens, starting with a letter or digit.';
  const FIELDS = {
    'Account address': 'https://Acme-Fin.snowflakecomputing.com',
    Package: 'Sales_Pkg',
    Application: 'SALES_APP$1',
    Warehouse: '_app_wh',
    'Cortex model': 'llama3.1-70b',
  };

  async function choose(platform: string) {
    await userEvent.selectOptions(screen.getByLabelText('Platform'), platform);
  }

  async function fillSnowflake(over: Partial<typeof FIELDS> = {}) {
    await userEvent.click(await screen.findByRole('button', { name: 'Register target' }));
    const dialog = screen.getByRole('dialog');
    await choose('Snowflake');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Finance prod');
    await within(dialog).findByRole('option', { name: 'Engineering' });
    await userEvent.selectOptions(within(dialog).getByLabelText('Workspace'), 'eng');
    for (const [label, value] of Object.entries({ ...FIELDS, ...over })) {
      await userEvent.type(within(dialog).getByLabelText(label), value);
    }
    await userEvent.type(within(dialog).getByLabelText('Credential reference'), ' sf.prod ');
    return dialog;
  }

  const submit = (dialog: HTMLElement) =>
    userEvent.click(within(dialog).getByRole('button', { name: 'Register target' }));

  it('swaps the platform fields, keeps the shared values and clears the shown errors', async () => {
    stubDelivery(WORKSPACES);
    renderWithClient(<TargetsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Register target' }));
    const dialog = screen.getByRole('dialog');
    const d = within(dialog);
    expect(d.getByLabelText('Platform')).toHaveDisplayValue('Databricks');
    expect(d.getByText('Experimental')).toBeInTheDocument();
    for (const label of Object.keys(FIELDS)) {
      expect(d.queryByLabelText(label)).not.toBeInTheDocument();
    }
    await userEvent.type(d.getByLabelText('Name'), 'Finance prod');
    await d.findByRole('option', { name: 'Engineering' });
    await userEvent.selectOptions(d.getByLabelText('Workspace'), 'eng');
    await d.findByRole('option', { name: 'API' });
    await userEvent.selectOptions(d.getByLabelText('Project (optional)'), 'api');
    await userEvent.selectOptions(d.getByLabelText('Environment'), 'prod');
    await submit(dialog);
    expect(await d.findByText('Warehouse ID is required.')).toBeInTheDocument();
    expect(d.getByText('Credential reference is required.')).toBeInTheDocument();

    await choose('Snowflake');
    expect(d.queryByText(/is required/)).not.toBeInTheDocument();
    for (const label of Object.keys(FIELDS)) {
      expect(d.getByLabelText(label)).toHaveValue('');
    }
    expect(d.getByText(/https:\/\/<account>\.snowflakecomputing\.com/)).toBeInTheDocument();
    expect(d.getByText('Experimental')).toBeInTheDocument();
    for (const label of ['Address', 'Warehouse ID', 'Serving endpoint (optional)']) {
      expect(d.queryByLabelText(label)).not.toBeInTheDocument();
    }
    expect(d.getByLabelText('Name')).toHaveValue('Finance prod');
    expect(d.getByLabelText('Workspace')).toHaveValue('eng');
    expect(d.getByLabelText('Project (optional)')).toHaveValue('api');
    expect(d.getByLabelText('Environment')).toHaveValue('prod');
  });

  it('posts exactly the Snowflake body, trimmed, closes the dialog and refreshes the list', async () => {
    const fetchMock = stubDelivery({
      ...WORKSPACES,
      [`POST ${D}/targets`]: () => jsonResponse(SNOWFLAKE_TARGET, { status: 201 }),
    });
    renderWithClient(<TargetsView />);
    const dialog = await fillSnowflake(
      Object.fromEntries(Object.entries(FIELDS).map(([k, v]) => [k, `  ${v} `])),
    );
    await submit(dialog);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/targets`)).toEqual({
      workspace: 'eng',
      name: 'Finance prod',
      platform_kind: 'snowflake',
      address: 'https://Acme-Fin.snowflakecomputing.com',
      environment: 'dev',
      bindings: {
        package: 'Sales_Pkg',
        application: 'SALES_APP$1',
        warehouse: '_app_wh',
        cortex_model: 'llama3.1-70b',
      },
      credential_ref: 'sf.prod',
    });
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `GET ${D}/targets`).length).toBe(2),
    );
  });

  it('posts no value typed for the other platform', async () => {
    const fetchMock = stubDelivery({
      ...WORKSPACES,
      [`POST ${D}/targets`]: () => jsonResponse(TARGET, { status: 201 }),
    });
    renderWithClient(<TargetsView />);
    const dialog = await fillSnowflake();
    await choose('Databricks');
    expect(within(dialog).getByLabelText('Address')).toHaveValue('');
    await userEvent.type(within(dialog).getByLabelText('Address'), 'https://adb-1.example');
    await userEvent.type(within(dialog).getByLabelText('Warehouse ID'), 'wh-1');
    await submit(dialog);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/targets`)).toEqual({
      workspace: 'eng',
      name: 'Finance prod',
      platform_kind: 'databricks',
      address: 'https://adb-1.example',
      environment: 'dev',
      bindings: { warehouse_id: 'wh-1' },
      credential_ref: 'sf.prod',
    });
  });

  it('asks for each Snowflake field before calling the API', async () => {
    const fetchMock = stubDelivery(WORKSPACES);
    renderWithClient(<TargetsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Register target' }));
    const dialog = screen.getByRole('dialog');
    await choose('Snowflake');
    await userEvent.type(within(dialog).getByLabelText('Package'), '   ');
    await submit(dialog);
    for (const label of Object.keys(FIELDS)) {
      expect(within(dialog).getByLabelText(label)).toHaveAccessibleDescription(
        new RegExp(`${label} is required\\.$`),
      );
    }
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it.each([
    ['Package', '1bad', IDENTIFIER_RULE],
    ['Application', 'a-b', IDENTIFIER_RULE],
    ['Warehouse', 'has space', IDENTIFIER_RULE],
    ['Cortex model', 'Llama', CORTEX_MODEL_RULE],
  ])('refuses the %s %j before calling the API', async (label, value, rule) => {
    const fetchMock = stubDelivery(WORKSPACES);
    renderWithClient(<TargetsView />);
    const dialog = await fillSnowflake({ [label]: value });
    await submit(dialog);
    expect(await within(dialog).findByText(rule)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(label)).toHaveAccessibleDescription(rule);
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it.each([
    ['platform_already_registered', CLAIMED],
    ['binding_in_use', 'This package or application is already bound to a target.'],
  ])('shows a 409 %s in the dialog until the platform changes', async (code, detail) => {
    stubDelivery({
      ...WORKSPACES,
      [`POST ${D}/targets`]: () => problemResponse(409, code, { detail }),
    });
    renderWithClient(<TargetsView />);
    const dialog = await fillSnowflake();
    await submit(dialog);
    const alert = await within(dialog).findByText(detail);
    expect(alert.closest('[role="alert"]')).toHaveTextContent('Could not register the target');
    expect(within(dialog).getByText(code)).toBeInTheDocument();
    await choose('Databricks');
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
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
