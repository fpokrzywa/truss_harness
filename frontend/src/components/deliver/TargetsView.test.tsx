import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { TargetOut } from '@/lib/api/delivery-types';
import {
  D,
  DELIVERY_READ_PERMS,
  DOCKER_TARGET,
  list,
  PALANTIR_BINDINGS,
  PALANTIR_TARGET,
  SNOWFLAKE_TARGET,
  stubDelivery,
  TARGET,
  TARGETS_OF_EACH_KIND,
  unusable,
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

/** The labelled bindings in the Bindings cell of the named row, or the cell's text without them. */
function bindingsOf(table: HTMLElement, name: string): Record<string, string | null> | string {
  const headers = within(table).getAllByRole('columnheader');
  const column = headers.findIndex((h) => h.textContent === 'Bindings');
  const row = within(table).getByRole('cell', { name }).closest('tr')!;
  const cell = within(row).getAllByRole('cell')[column]!;
  const terms = Array.from(cell.querySelectorAll('dt'));
  if (terms.length === 0) return cell.textContent ?? '';
  return Object.fromEntries(
    terms.map((dt) => [dt.textContent, dt.nextElementSibling!.textContent]),
  );
}

async function fillRegister() {
  await userEvent.click(await screen.findByRole('button', { name: 'Register target' }));
  const dialog = screen.getByRole('dialog');
  await userEvent.selectOptions(within(dialog).getByLabelText('Platform'), 'Databricks');
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

  it('shows the serving endpoint in the Bindings cell, and - when the target has none', async () => {
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
    expect(within(table).getByRole('columnheader', { name: 'Bindings' })).toBeInTheDocument();
    expect(bindingsOf(table, 'Analytics prod')).toEqual({
      'Warehouse ID': 'wh-0042',
      'Serving endpoint': 'llama-serving',
    });
    expect(bindingsOf(table, 'Analytics dev')).toEqual({
      'Warehouse ID': 'wh-0042',
      'Serving endpoint': '-',
    });
  });

  it('shows the bindings of each platform in its Bindings cell, and none of the other platform', async () => {
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
      'Credential reference': 'sf.prod',
    });
    expect(bindingsOf(table, 'Finance prod')).toEqual({
      Package: 'SALES_PKG',
      Application: 'SALES_APP',
      Warehouse: 'APP_WH',
      'Cortex model': 'llama3.1-70b',
    });
    expect(cellsOf('Analytics prod')).toMatchObject({
      Kind: 'DatabricksExperimental',
      'Credential reference': 'dbx.prod',
    });
    expect(bindingsOf(table, 'Analytics prod')).toEqual({
      'Warehouse ID': 'wh-0042',
      'Serving endpoint': '-',
    });
    expect(headers).not.toContain('Warehouse ID');
    expect(headers).not.toContain('Warehouse');
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
    await userEvent.selectOptions(within(dialog).getByLabelText('Platform'), 'Databricks');
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
    expect(bindingsOf(table, 'Analytics prod')).toEqual({
      'Warehouse ID': 'wh-0042',
      'Serving endpoint': 'llama_serving-01',
    });
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
    expect(d.getByLabelText('Platform')).toHaveDisplayValue('Select...');
    expect(d.queryByText('Experimental')).not.toBeInTheDocument();
    await choose('Databricks');
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

const LABELS = ['Databricks', 'Snowflake', 'Microsoft Fabric', 'Palantir Foundry', 'Docker'];
const UNUSABLE_ACTIVE =
  "This target's stored settings are no longer valid, so nothing can be planned, deployed or reconciled on it. Archive it and register it again.";
const UNUSABLE_ARCHIVED = "This target's stored settings are no longer valid.";
const PALANTIR_SHOWN = {
  'Marketplace RID': PALANTIR_BINDINGS.marketplace_rid,
  'Product ID': PALANTIR_BINDINGS.product_id,
  'Version ID': PALANTIR_BINDINGS.version_id,
  'Namespace RID': PALANTIR_BINDINGS.namespace_rid,
  'Parent folder RID': PALANTIR_BINDINGS.parent_folder_rid,
  'Ontology RID': PALANTIR_BINDINGS.ontology_rid,
  'Object type': 'sales.Order',
  'Frontend origin': 'https://sales.example.com',
  'Frontend asset SHA-256': 'e'.repeat(64),
  'Preview APIs': 'Allowed',
};

describe('TargetsView list of every platform', () => {
  it('shows each target with its label, Experimental and exactly its own bindings', async () => {
    const noPreview = {
      ...PALANTIR_TARGET,
      id: 't-pal-2',
      name: 'Foundry dev',
      bindings: { ...PALANTIR_BINDINGS, allow_preview_apis: false },
    };
    stubDelivery({ [`GET ${D}/targets`]: () => list([...TARGETS_OF_EACH_KIND, noPreview]) });
    renderWithClient(<TargetsView />);
    const table = await screen.findByRole('table', { name: 'Deployment targets' });
    const expected: [string, string, Record<string, string>][] = [
      ['Analytics prod', 'Databricks', { 'Warehouse ID': 'wh-0042', 'Serving endpoint': '-' }],
      [
        'Finance prod',
        'Snowflake',
        {
          Package: 'SALES_PKG',
          Application: 'SALES_APP',
          Warehouse: 'APP_WH',
          'Cortex model': 'llama3.1-70b',
        },
      ],
      [
        'Fabric prod',
        'Microsoft Fabric',
        {
          'Entra tenant ID': 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
          'Lakehouse name': 'sales_lake',
          'Notebook name': 'sales_notebook',
        },
      ],
      ['Foundry prod', 'Palantir Foundry', PALANTIR_SHOWN],
      ['Foundry dev', 'Palantir Foundry', { ...PALANTIR_SHOWN, 'Preview APIs': 'Not allowed' }],
      [
        'Docker prod',
        'Docker',
        {
          Project: 'sales-app',
          Port: '8443',
          'Databricks workspace': 'https://adb-123.azuredatabricks.net',
          'Warehouse ID': 'wh-0042',
          'Serving endpoint': 'llama-serving',
        },
      ],
    ];
    for (const [name, label, bindings] of expected) {
      const row = within(table).getByRole('cell', { name }).closest('tr')!;
      const kind = within(row).getAllByRole('cell')[1]!;
      expect(kind).toHaveTextContent(new RegExp(`^${label}Experimental$`));
      expect(bindingsOf(table, name)).toEqual(bindings);
      for (const other of LABELS.filter((l) => l !== label)) {
        expect(within(row).queryByText(other)).not.toBeInTheDocument();
      }
    }
    expect(screen.queryByText('Validated')).not.toBeInTheDocument();
  });

  it('scrolls sideways inside its card, keeps Archive in view and wraps long values in wide cells', async () => {
    stubDelivery({ [`GET ${D}/targets`]: () => list(TARGETS_OF_EACH_KIND) });
    renderWithClient(<TargetsView />);
    const scroll = await screen.findByRole('region', { name: 'Deployment targets list' });
    expect(scroll).toHaveClass('card', 'overflow-x-auto');
    expect(scroll).toHaveAttribute('tabindex', '0');
    const table = within(scroll).getByRole('table', { name: 'Deployment targets' });
    const headers = within(table).getAllByRole('columnheader');
    expect(headers.at(-1)).toHaveClass('sticky', 'right-0');
    const address = headers.findIndex((h) => h.textContent === 'Address');
    const bindings = headers.findIndex((h) => h.textContent === 'Bindings');
    const buttons = within(table).getAllByRole('button', { name: /Archive/ });
    expect(buttons).toHaveLength(TARGETS_OF_EACH_KIND.length);
    for (const t of TARGETS_OF_EACH_KIND) {
      const row = within(table).getByRole('cell', { name: t.name }).closest('tr')!;
      const cells = within(row).getAllByRole('cell');
      expect(cells.at(-1)).toHaveClass('sticky', 'right-0');
      expect(within(cells.at(-1)!).getByRole('button', { name: /Archive/ })).toBeVisible();
      expect(cells[address]).toHaveClass('min-w-48', 'break-all');
      expect(cells[bindings]).toHaveClass('min-w-64');
    }
  });

  it('shows a target of an unknown kind with its raw kind, - and no binding or platform name', async () => {
    const mystery = {
      ...TARGET,
      id: 't-m',
      name: 'Mystery prod',
      platform_kind: 'mystery',
      bindings: { warehouse_id: 'wh-secret-9', gizmo: 'gizmo-1' },
    } as unknown as TargetOut;
    stubDelivery({ [`GET ${D}/targets`]: () => list([mystery]) });
    renderWithClient(<TargetsView />);
    const table = await screen.findByRole('table', { name: 'Deployment targets' });
    const row = within(table).getByRole('cell', { name: 'Mystery prod' }).closest('tr')!;
    expect(within(row).getAllByRole('cell')[1]).toHaveTextContent(/^mystery$/);
    expect(bindingsOf(table, 'Mystery prod')).toBe('-');
    for (const word of [...LABELS, 'wh-secret-9', 'gizmo-1', 'Experimental']) {
      expect(within(row).queryByText(new RegExp(word))).not.toBeInTheDocument();
    }
  });

  it.each(TARGETS_OF_EACH_KIND.map((t) => [t.platform_kind, t] as const))(
    'shows the unusable hint on an active %s target, archives it, and the short text once archived',
    async (_kind, target) => {
      const broken = unusable(target, { id: 't-bad', name: 'Broken target' });
      const archived = unusable(target, { id: 't-old', name: 'Old target', status: 'archived' });
      const fetchMock = stubDelivery({
        [`GET ${D}/targets`]: () => list([broken, archived, SNOWFLAKE_TARGET]),
        [`POST ${D}/targets/t-bad/archive`]: () => jsonResponse({ ...broken, status: 'archived' }),
      });
      const { container } = renderWithClient(<TargetsView />);
      const table = await screen.findByRole('table', { name: 'Deployment targets' });
      expect(bindingsOf(table, 'Broken target')).toBe(UNUSABLE_ACTIVE);
      expect(bindingsOf(table, 'Old target')).toBe(UNUSABLE_ARCHIVED);
      expect(bindingsOf(table, 'Finance prod')).toMatchObject({ Package: 'SALES_PKG' });
      const row = within(table).getByRole('cell', { name: 'Broken target' }).closest('tr')!;
      expect(within(row).getByText('Experimental')).toBeInTheDocument();
      expect(container).not.toHaveTextContent('undefined');
      await userEvent.click(within(row).getByRole('button', { name: /Archive/ }));
      await userEvent.click(screen.getByRole('button', { name: 'Archive target' }));
      await waitFor(() =>
        expect(fetchMock.calls.some((c) => c.key === `POST ${D}/targets/t-bad/archive`)).toBe(true),
      );
    },
  );
});

type KindCase = {
  label: string;
  address: [string, string];
  fields: Record<string, string>;
  bindings: Record<string, unknown>;
};

const FABRIC: KindCase = {
  label: 'Microsoft Fabric',
  address: ['Fabric workspace', '11111111-2222-4333-8444-555555555555'],
  fields: {
    'Entra tenant ID': 'AAAAAAAA-bbbb-4ccc-8ddd-eeeeeeeeeeee',
    'Lakehouse name': 'sales_lake',
    'Notebook name': 'sales_notebook',
  },
  bindings: {
    entra_tenant_id: 'AAAAAAAA-bbbb-4ccc-8ddd-eeeeeeeeeeee',
    lakehouse_name: 'sales_lake',
    notebook_name: 'sales_notebook',
  },
};
const PALANTIR: KindCase = {
  label: 'Palantir Foundry',
  address: ['Foundry stack address', 'https://acme.palantirfoundry.com'],
  fields: Object.fromEntries(
    Object.entries(PALANTIR_SHOWN).filter(([label]) => label !== 'Preview APIs'),
  ),
  bindings: { ...PALANTIR_BINDINGS, allow_preview_apis: false },
};
const DOCKER: KindCase = {
  label: 'Docker',
  address: [
    'Docker daemon address',
    'docker://ABCD:EFGH:IJKL:MNOP:QRST:UVWX:YZ23:4567:ABCD:EFGH:IJKL:MNOP',
  ],
  fields: {
    Project: 'sales-app',
    Port: '8443',
    'Databricks workspace': 'https://adb-123.azuredatabricks.net',
    'Warehouse ID': 'wh-0042',
    'Serving endpoint': 'llama-serving',
  },
  bindings: {
    project: 'sales-app',
    port: 8443,
    workspace_host: 'https://adb-123.azuredatabricks.net',
    warehouse_id: 'wh-0042',
    serving_endpoint: 'llama-serving',
  },
};
const KIND_CASES = { fabric: FABRIC, palantir: PALANTIR, docker: DOCKER };

describe('TargetsView register on Fabric, Palantir and Docker', () => {
  const submit = (dialog: HTMLElement) =>
    userEvent.click(within(dialog).getByRole('button', { name: 'Register target' }));

  async function open() {
    await userEvent.click(await screen.findByRole('button', { name: 'Register target' }));
    return screen.getByRole('dialog');
  }

  /** Chooses the kind and fills every field, each value wrapped in spaces unless `over` sets it. */
  async function fill(c: KindCase, over: Record<string, string> = {}) {
    const dialog = await open();
    const d = within(dialog);
    await userEvent.selectOptions(d.getByLabelText('Platform'), c.label);
    await userEvent.type(d.getByLabelText('Name'), ' Target prod ');
    await d.findByRole('option', { name: 'Engineering' });
    await userEvent.selectOptions(d.getByLabelText('Workspace'), 'eng');
    const values = { [c.address[0]]: c.address[1], ...c.fields };
    for (const [label, value] of Object.entries(values)) {
      // pasted: typing ten long Foundry values key by key outlasts the test timeout
      await userEvent.click(d.getByLabelText(label));
      await userEvent.paste(over[label] ?? `  ${value} `);
    }
    await userEvent.type(d.getByLabelText('Credential reference'), ' target.prod ');
    return dialog;
  }

  it('opens with no platform chosen and asks for one before calling the API', async () => {
    const fetchMock = stubDelivery(WORKSPACES);
    renderWithClient(<TargetsView />);
    const dialog = await open();
    const d = within(dialog);
    const select = d.getByLabelText('Platform');
    expect(select).toHaveDisplayValue('Select...');
    expect(
      within(select)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Select...', ...LABELS]);
    for (const label of ['Address', 'Account address', 'Fabric workspace', 'Warehouse ID']) {
      expect(d.queryByLabelText(label)).not.toBeInTheDocument();
    }
    expect(d.queryByText('Experimental')).not.toBeInTheDocument();
    await submit(dialog);
    expect(await d.findByText('Choose a platform.')).toBeInTheDocument();
    expect(d.getByText('Name is required.')).toBeInTheDocument();
    expect(d.getByText('Credential reference is required.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it.each([
    ['fabric', false, FABRIC],
    ['palantir', false, PALANTIR],
    ['palantir', true, PALANTIR],
    ['docker', false, DOCKER],
  ] as const)(
    'posts exactly the %s body (preview APIs ticked: %s), trimmed, and refreshes the list',
    async (kind, preview, c) => {
      const fetchMock = stubDelivery({
        ...WORKSPACES,
        [`POST ${D}/targets`]: () => jsonResponse(TARGET, { status: 201 }),
      });
      renderWithClient(<TargetsView />);
      const dialog = await fill(c);
      const d = within(dialog);
      expect(d.getByText('Experimental')).toBeInTheDocument();
      if (kind === 'palantir') {
        const box = d.getByRole('checkbox', { name: 'Allow Foundry preview APIs' });
        expect(box).not.toBeChecked();
        expect(
          d.getByText(
            'Palantir installs use Foundry preview APIs. Without this, every plan on this target is refused.',
          ),
        ).toBeInTheDocument();
        if (preview) await userEvent.click(box);
      }
      await submit(dialog);
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(bodyOf(fetchMock, `POST ${D}/targets`)).toEqual({
        workspace: 'eng',
        name: 'Target prod',
        platform_kind: kind,
        address: c.address[1],
        environment: 'dev',
        bindings: kind === 'palantir' ? { ...c.bindings, allow_preview_apis: preview } : c.bindings,
        credential_ref: 'target.prod',
      });
      await waitFor(() =>
        expect(fetchMock.calls.filter((call) => call.key === `GET ${D}/targets`).length).toBe(2),
      );
    },
  );

  it.each(Object.entries(KIND_CASES))(
    'names every required %s field before calling the API',
    async (_kind, c) => {
      const fetchMock = stubDelivery(WORKSPACES);
      renderWithClient(<TargetsView />);
      const dialog = await open();
      await userEvent.selectOptions(within(dialog).getByLabelText('Platform'), c.label);
      await submit(dialog);
      for (const label of [c.address[0], ...Object.keys(c.fields)]) {
        expect(within(dialog).getByLabelText(label)).toHaveAccessibleDescription(
          new RegExp(`${label} is required\\.$`),
        );
      }
      expect(fetchMock.calls.some((call) => call.key.startsWith('POST'))).toBe(false);
    },
  );

  const RID = (kind: string) => `ri.compass.main.${kind}.00000000-0000-4000-8000-000000000009`;
  const UUID_RULE = 'Use a UUID: 8-4-4-4-12 hexadecimal digits with hyphens.';
  const PORT_RULE = 'Use a whole number from 1024 to 65535.';
  it.each([
    [
      'fabric',
      'Entra tenant ID',
      'aaaa-bbbb',
      'Use a GUID: 8-4-4-4-12 hexadecimal digits with hyphens.',
    ],
    [
      'fabric',
      'Lakehouse name',
      '1lake',
      'Use 1-100 letters, digits or underscores, starting with a letter.',
    ],
    [
      'fabric',
      'Notebook name',
      'sales_lake',
      'The lakehouse and the notebook need different names.',
    ],
    [
      'palantir',
      'Marketplace RID',
      RID('folder'),
      'Use the RID of the Marketplace: ri.<service>.<instance>.local.<id>.',
    ],
    [
      'palantir',
      'Namespace RID',
      RID('ontology'),
      'Use the RID of the namespace: ri.<service>.<instance>.namespace.<id>.',
    ],
    [
      'palantir',
      'Parent folder RID',
      RID('namespace'),
      'Use the RID of the parent folder: ri.<service>.<instance>.folder.<id>.',
    ],
    [
      'palantir',
      'Ontology RID',
      RID('local'),
      'Use the RID of the ontology: ri.<service>.<instance>.ontology.<id>.',
    ],
    ['palantir', 'Product ID', 'not-a-uuid', UUID_RULE],
    ['palantir', 'Version ID', '00000000-0000-4000-8000-00000000000', UUID_RULE],
    [
      'palantir',
      'Object type',
      '1Order',
      'Use 1-128 letters, digits, underscores or dots, starting with a letter.',
    ],
    ['palantir', 'Frontend origin', 'http://sales.example.com', 'Use an https:// origin.'],
    ['palantir', 'Frontend asset SHA-256', 'E'.repeat(64), 'Use 64 lowercase hexadecimal digits.'],
    ['docker', 'Port', '1023', PORT_RULE],
    ['docker', 'Port', '65536', PORT_RULE],
    ['docker', 'Port', '80a', PORT_RULE],
    [
      'docker',
      'Project',
      'Sales_App',
      'Use 1-40 lowercase letters, digits or hyphens, starting with a letter.',
    ],
    [
      'docker',
      'Databricks workspace',
      'https://adb-123.example.net/path',
      'Use the workspace URL: https:// and a lowercase host name, with no path.',
    ],
    ['docker', 'Warehouse ID', 'wh 42', 'Use 1-64 letters, digits or hyphens.'],
    ['docker', 'Serving endpoint', 'bad.name', 'Use 1-63 letters, digits, underscores or hyphens.'],
  ] as const)(
    'refuses a bad %s %s, %j, before calling the API',
    async (kind, label, value, rule) => {
      const fetchMock = stubDelivery(WORKSPACES);
      renderWithClient(<TargetsView />);
      const dialog = await fill(KIND_CASES[kind], { [label]: value });
      await submit(dialog);
      expect(await within(dialog).findByText(rule)).toBeInTheDocument();
      expect(within(dialog).getByLabelText(label)).toHaveAccessibleDescription(rule);
      expect(fetchMock.calls.some((call) => call.key.startsWith('POST'))).toBe(false);
    },
  );

  const DAEMON_HELP =
    "docker://<daemon id>. The id is the output of docker info --format '{{.ID}}' on the host: a lowercase UUID, or twelve groups of four capital letters A-Z or digits 2-7 joined by colons.";
  const DAEMON_RULE =
    'Use docker:// and the daemon id: a lowercase UUID, or twelve groups of four capital letters A-Z or digits 2-7 joined by colons.';

  it.each([
    'docker://0a1b2c3d-1111-4222-8333-444455556666',
    'docker://ABCD:EFGH:IJKL:MNOP:QRST:UVWX:YZ23:4567:ABCD:EFGH:IJKL:MNOP',
  ])('posts the Docker daemon address %s as typed', async (address) => {
    const fetchMock = stubDelivery({
      ...WORKSPACES,
      [`POST ${D}/targets`]: () => jsonResponse(DOCKER_TARGET, { status: 201 }),
    });
    renderWithClient(<TargetsView />);
    const dialog = await fill(DOCKER, { 'Docker daemon address': ` ${address} ` });
    expect(within(dialog).getByLabelText('Docker daemon address')).toHaveAccessibleDescription(
      DAEMON_HELP,
    );
    await submit(dialog);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/targets`).address).toBe(address);
  });

  it.each([
    ['a free-form id', 'docker://walkdaemon123'],
    ['an upper-case UUID', 'docker://0A1B2C3D-1111-4222-8333-444455556666'],
    [
      'a lower-case base32 id',
      'docker://abcd:efgh:ijkl:mnop:qrst:uvwx:yz23:4567:abcd:efgh:ijkl:mnop',
    ],
    ['eleven base32 groups', 'docker://ABCD:EFGH:IJKL:MNOP:QRST:UVWX:YZ23:4567:ABCD:EFGH:IJKL'],
    ['base32 with a 1', 'docker://ABC1:EFGH:IJKL:MNOP:QRST:UVWX:YZ23:4567:ABCD:EFGH:IJKL:MNOP'],
    ['a UUID without hyphens', 'docker://0a1b2c3d111142228333444455556666'],
    ['no scheme', '0a1b2c3d-1111-4222-8333-444455556666'],
    ['another scheme', 'tcp://0a1b2c3d-1111-4222-8333-444455556666'],
    ['a trailing path', 'docker://0a1b2c3d-1111-4222-8333-444455556666/'],
  ])('refuses %s as the Docker daemon address before calling the API', async (_case, address) => {
    const fetchMock = stubDelivery(WORKSPACES);
    renderWithClient(<TargetsView />);
    const dialog = await fill(DOCKER, { 'Docker daemon address': address });
    await submit(dialog);
    expect(await within(dialog).findByText(DAEMON_RULE)).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Docker daemon address')).toHaveAccessibleDescription(
      `${DAEMON_HELP} ${DAEMON_RULE}`,
    );
    expect(fetchMock.calls.some((call) => call.key.startsWith('POST'))).toBe(false);
  });

  it('posts no value typed for the kind it switched from and keeps the shared fields', async () => {
    const fetchMock = stubDelivery({
      ...WORKSPACES,
      [`POST ${D}/targets`]: () => jsonResponse(TARGET, { status: 201 }),
    });
    renderWithClient(<TargetsView />);
    const dialog = await fill(DOCKER);
    const d = within(dialog);
    await userEvent.selectOptions(d.getByLabelText('Environment'), 'test');
    await userEvent.selectOptions(d.getByLabelText('Platform'), 'Databricks');
    expect(d.getByLabelText('Address')).toHaveValue('');
    expect(d.getByLabelText('Warehouse ID')).toHaveValue('');
    expect(d.getByLabelText('Serving endpoint (optional)')).toHaveValue('');
    expect(d.getByLabelText('Name')).toHaveValue(' Target prod ');
    expect(d.getByLabelText('Workspace')).toHaveValue('eng');
    expect(d.getByLabelText('Environment')).toHaveValue('test');
    expect(d.getByLabelText('Credential reference')).toHaveValue(' target.prod ');
    await userEvent.type(d.getByLabelText('Address'), 'https://adb-1.example');
    await userEvent.type(d.getByLabelText('Warehouse ID'), 'wh-1');
    await submit(dialog);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/targets`)).toEqual({
      workspace: 'eng',
      name: 'Target prod',
      platform_kind: 'databricks',
      address: 'https://adb-1.example',
      environment: 'test',
      bindings: { warehouse_id: 'wh-1' },
      credential_ref: 'target.prod',
    });
  });

  it('clears a shown 409 and the field errors when the kind changes', async () => {
    stubDelivery({
      ...WORKSPACES,
      [`POST ${D}/targets`]: () =>
        problemResponse(409, 'platform_already_registered', { detail: CLAIMED }),
    });
    renderWithClient(<TargetsView />);
    const dialog = await fill(FABRIC);
    await submit(dialog);
    expect(await within(dialog).findByText(CLAIMED)).toBeInTheDocument();
    expect(within(dialog).getByText('platform_already_registered')).toBeInTheDocument();
    await userEvent.selectOptions(within(dialog).getByLabelText('Platform'), 'Palantir Foundry');
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    await submit(dialog);
    expect(await within(dialog).findByText('Marketplace RID is required.')).toBeInTheDocument();
    await userEvent.selectOptions(within(dialog).getByLabelText('Platform'), 'Docker');
    expect(within(dialog).queryByText(/is required/)).not.toBeInTheDocument();
  });
});
