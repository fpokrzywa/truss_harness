import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { artifact, version } from '@/test/build';
import { DS, list, ORG, RELEASE, S, SOLUTION, stubReleases, VERSION } from '@/test/releases';
import { ALL_PERMS, bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { SolutionDetailView } from './SolutionDetailView';

const nav = vi.hoisted(() => ({ query: 'ws=eng&proj=api', push: vi.fn() }));
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(nav.query),
  usePathname: () => '/o/acme/solutions/s-1',
  useRouter: () => ({ push: nav.push, replace: vi.fn() }),
}));
beforeEach(() => nav.push.mockReset());
afterEach(() => vi.unstubAllGlobals());

const connection = (id: string, name: string, connection_type: string) => ({
  id,
  name,
  connection_type,
  adapter_key: 'x',
  config: {},
  auth_method: 'none',
  access_mode: 'read',
  status: 'active',
  row_version: 1,
});

const LOOKUPS = {
  [`GET ${ORG}/agents`]: () =>
    list([artifact({ id: 'ag-1', key: 'sales.agent', name: 'Sales agent' })]),
  [`GET ${ORG}/agents/ag-1/versions`]: () => list([version({ id: 'av-1' })]),
  [`GET ${ORG}/connections`]: () =>
    list([
      connection('conn-db', 'Warehouse', 'data_platform'),
      connection('conn-crm', 'CRM', 'rest'),
    ]),
  [`GET ${ORG}/model-providers`]: () =>
    list([
      { id: 'prov-1', name: 'Azure OpenAI', adapter_key: 'a', connection_id: 'x', row_version: 1 },
    ]),
};

const SEAL = `POST ${S}/s-1/versions/sv-1/releases`;

describe('SolutionDetailView', () => {
  it('renders the header, the versions and each version releases with release links', async () => {
    stubReleases(LOOKUPS);
    renderWithClient(<SolutionDetailView id="s-1" />);
    expect(await screen.findByRole('heading', { name: 'Sales assistant' })).toBeInTheDocument();
    expect(screen.getByText('eng / api')).toBeInTheDocument();
    const versions = await screen.findByRole('table', { name: 'Versions' });
    const row = within(versions).getByText('v1').closest('tr')!;
    expect(within(row).getByText('agent')).toBeInTheDocument();
    expect(within(row).getByText('2')).toBeInTheDocument();
    expect(within(row).getByText('Set')).toBeInTheDocument();
    const releases = await screen.findByRole('table', { name: 'Releases of v1' });
    const link = within(releases).getByRole('link', { name: 'abcdef012345' });
    expect(link).toHaveAttribute('href', '/o/acme/releases/r-1');
    expect(link).toHaveAttribute('title', RELEASE.digest);
  });

  it('asks for a project when the URL names none', async () => {
    nav.query = '';
    const fetchMock = stubReleases(LOOKUPS);
    renderWithClient(<SolutionDetailView id="s-1" />);
    expect(await screen.findByText(/This link does not name a project/)).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.includes('/solutions'))).toBe(false);
    nav.query = 'ws=eng&proj=api';
  });

  it('has no New version or Seal release buttons without write and seal permissions', async () => {
    stubReleases(LOOKUPS, [...ALL_PERMS, 'packaging:solution:read']);
    renderWithClient(<SolutionDetailView id="s-1" />);
    await screen.findByRole('table', { name: 'Versions' });
    expect(screen.queryByRole('button', { name: 'New version' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Seal release/ })).not.toBeInTheDocument();
  });

  it('has no New version button on an archived solution', async () => {
    stubReleases({
      ...LOOKUPS,
      [`GET ${S}/s-1`]: () => jsonResponse({ ...SOLUTION, status: 'archived' }),
    });
    renderWithClient(<SolutionDetailView id="s-1" />);
    await screen.findByRole('table', { name: 'Versions' });
    expect(screen.queryByRole('button', { name: 'New version' })).not.toBeInTheDocument();
  });
});

describe('SolutionDetailView new version', () => {
  it('blocks the submit without an entrypoint, a data read connection or valid thresholds', async () => {
    const fetchMock = stubReleases(LOOKUPS);
    renderWithClient(<SolutionDetailView id="s-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'New version' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add data read' }));
    await userEvent.click(within(dialog).getByLabelText('Set an evaluation contract'));
    await userEvent.type(within(dialog).getByLabelText('Min accuracy'), '1.5');
    await userEvent.type(within(dialog).getByLabelText('Max p95 latency (ms)'), '0');
    await userEvent.type(within(dialog).getByLabelText('Max total cost (USD)'), 'abc');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create version' }));
    expect(
      await within(dialog).findByText('Choose a published entrypoint version.'),
    ).toBeInTheDocument();
    expect(within(dialog).getByText('Choose a connection.')).toBeInTheDocument();
    expect(within(dialog).getByText('List at least one table.')).toBeInTheDocument();
    expect(within(dialog).getByText('Choose a dataset.')).toBeInTheDocument();
    expect(within(dialog).getByText('Enter a decimal between 0 and 1.')).toBeInTheDocument();
    expect(
      within(dialog).getByText('Enter a positive whole number of milliseconds.'),
    ).toBeInTheDocument();
    expect(within(dialog).getByText('Enter a decimal amount in USD.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('posts the entrypoint, data reads split by line and the thresholds as typed', async () => {
    const fetchMock = stubReleases({
      ...LOOKUPS,
      [`POST ${S}/s-1/versions`]: () => jsonResponse(VERSION, { status: 201 }),
    });
    renderWithClient(<SolutionDetailView id="s-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'New version' }));
    const dialog = screen.getByRole('dialog');
    const d = within(dialog);
    await d.findByRole('option', { name: 'Sales agent (sales.agent)' });
    await userEvent.selectOptions(d.getByLabelText('Entrypoint: artifact'), 'ag-1');
    await d.findByRole('option', { name: 'Pinned v1' });
    await userEvent.selectOptions(d.getByLabelText('Entrypoint: version'), 'av-1');
    await userEvent.click(d.getByRole('button', { name: 'Add data read' }));
    await d.findByRole('option', { name: 'Warehouse (data_platform)' });
    expect(d.queryByRole('option', { name: 'CRM (rest)' })).not.toBeInTheDocument();
    await userEvent.selectOptions(d.getByLabelText('Data read 1: connection'), 'conn-db');
    await userEvent.type(
      d.getByLabelText('Data read 1: tables'),
      'sales.orders{enter}sales.customers',
    );
    await userEvent.click(d.getByLabelText('Set an evaluation contract'));
    await d.findByRole('option', { name: 'Sales FAQ v2' });
    await userEvent.selectOptions(d.getByLabelText('Dataset'), 'ds-1');
    await userEvent.type(d.getByLabelText('Min accuracy'), '0.90');
    await userEvent.type(d.getByLabelText('Max p95 latency (ms)'), '2000');
    await userEvent.type(d.getByLabelText('Max total cost (USD)'), '1.50');
    await userEvent.click(d.getByRole('button', { name: 'Create version' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${S}/s-1/versions`)).toEqual({
      entrypoint_kind: 'agent',
      entrypoint_version_id: 'av-1',
      data_reads: [{ connection_id: 'conn-db', tables: ['sales.orders', 'sales.customers'] }],
      evaluation: {
        dataset_id: 'ds-1',
        min_accuracy: '0.90',
        max_p95_latency_ms: 2000,
        max_total_cost_usd: '1.50',
      },
    });
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `GET ${S}/s-1/versions`).length).toBe(2),
    );
    expect(fetchMock.calls.some((c) => c.key === `GET ${DS}`)).toBe(true);
  });
});

describe('SolutionDetailView seal', () => {
  it('shows the refused compatibility report and adds a binding row for the missing model pin', async () => {
    const fetchMock = stubReleases({
      ...LOOKUPS,
      [SEAL]: () =>
        problemResponse(422, 'release_incompatible', {
          detail: 'The release is not compatible with its target.',
          errors: [
            { loc: ['compatibility', 'entrypoint'], msg: 'supported', type: 'supported' },
            { loc: ['compatibility', 'tools', 't1'], msg: 'writes', type: 'unsupported' },
            {
              loc: ['compatibility', 'models', 'm1'],
              msg: 'no model binding',
              type: 'unsupported',
            },
          ],
        }),
    });
    renderWithClient(<SolutionDetailView id="s-1" />);
    await userEvent.click(await screen.findByRole('button', { name: /Seal release/ }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Seal release' }));
    const report = await within(dialog).findByRole('table', { name: 'Compatibility report' });
    const rows = within(report).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent('entrypoint');
    expect(rows[0]).toHaveTextContent('Supported');
    expect(rows[1]).toHaveTextContent('tools.t1');
    expect(rows[1]).toHaveTextContent('Not supported');
    expect(rows[1]).toHaveTextContent('writes');
    expect(rows[2]).toHaveTextContent('models.m1');
    expect(rows[2]).toHaveTextContent('no model binding');
    expect(within(dialog).getByLabelText('Model pin 1')).toHaveValue('m1');
    expect(within(dialog).queryByLabelText('Model pin 2')).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(bodyOf(fetchMock, SEAL)).toEqual({
      environment_bindings: { model_bindings: {}, connection_bindings: {} },
    });
  });

  it('shows any other refusal through the generic error alert', async () => {
    stubReleases({
      ...LOOKUPS,
      [SEAL]: () =>
        problemResponse(422, 'validation_failed', {
          detail: 'The request is not valid.',
          errors: [
            {
              loc: ['body', 'environment_bindings', 'model_bindings', 'zz'],
              msg: 'no such pin',
              type: 'value_error',
            },
          ],
        }),
    });
    renderWithClient(<SolutionDetailView id="s-1" />);
    await userEvent.click(await screen.findByRole('button', { name: /Seal release/ }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Seal release' }));
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('Could not seal the release');
    expect(alert).toHaveTextContent('no such pin');
    expect(within(dialog).queryByRole('table', { name: 'Compatibility report' })).toBeNull();
  });

  it.each([201, 200])(
    'posts the model and connection bindings and goes to the release on a %i',
    async (status) => {
      const fetchMock = stubReleases({
        ...LOOKUPS,
        [SEAL]: () => jsonResponse(RELEASE, { status }),
      });
      renderWithClient(<SolutionDetailView id="s-1" />);
      await userEvent.click(await screen.findByRole('button', { name: /Seal release/ }));
      const d = within(screen.getByRole('dialog'));
      await userEvent.click(d.getByRole('button', { name: 'Add model binding' }));
      await userEvent.type(d.getByLabelText('Model pin 1'), 'm1');
      await d.findByRole('option', { name: 'Azure OpenAI' });
      await userEvent.selectOptions(d.getByLabelText('Provider 1'), 'prov-1');
      await userEvent.type(d.getByLabelText('Model 1'), 'gpt-prod');
      await userEvent.click(d.getByRole('button', { name: 'Add connection binding' }));
      await userEvent.type(d.getByLabelText('Connection pin 1'), 'c1');
      await d.findByRole('option', { name: 'CRM (rest)' });
      await userEvent.selectOptions(d.getByLabelText('Connection 1'), 'conn-crm');
      await userEvent.click(d.getByRole('button', { name: 'Seal release' }));
      await waitFor(() => expect(nav.push).toHaveBeenCalledWith('/o/acme/releases/r-1'));
      expect(bodyOf(fetchMock, SEAL)).toEqual({
        environment_bindings: {
          model_bindings: { m1: { provider_id: 'prov-1', model: 'gpt-prod' } },
          connection_bindings: { c1: 'conn-crm' },
        },
      });
    },
  );
});
