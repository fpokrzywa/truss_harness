import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ADAPTER_PLANNED, stubInt } from '@/test/integrate';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { ProviderDetailView } from './ProviderDetailView';
import { ProvidersView } from './ProvidersView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const PROVIDER = {
  id: 'p-1',
  name: 'Local fake',
  adapter_key: 'fake',
  connection_id: 'c-1',
  health_status: 'healthy',
  health_checked_at: '2026-05-01T10:00:00Z',
  row_version: 1,
};

describe('ProvidersView', () => {
  it('creates a provider from an adapter and a model_api connection', async () => {
    const fetchMock = stubInt({
      'GET /api/v1/orgs/acme/model-providers': () => list([]),
      'GET /api/v1/orgs/acme/connections': () =>
        list([{ id: 'c-1', name: 'Fake endpoint', connection_type: 'model_api' }]),
      'POST /api/v1/orgs/acme/model-providers': () => jsonResponse(PROVIDER, { status: 201 }),
    });
    renderWithClient(<ProvidersView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create provider' }));
    const dlg = screen.getByRole('dialog');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Create provider' }));
    expect(within(dlg).getByText('Name is required.')).toBeInTheDocument();
    await userEvent.type(within(dlg).getByLabelText('Name'), 'Local fake');
    await screen.findByRole('option', { name: 'fake' });
    await userEvent.selectOptions(within(dlg).getByLabelText('Adapter'), 'fake');
    await screen.findByRole('option', { name: 'Fake endpoint' });
    await userEvent.selectOptions(within(dlg).getByLabelText('Model API connection'), 'c-1');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Create provider' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/model-providers')).toEqual({
      name: 'Local fake',
      adapter_key: 'fake',
      connection_id: 'c-1',
    });
    const conns = fetchMock.calls.find((c) => c.key === 'GET /api/v1/orgs/acme/connections');
    expect(conns?.url).toContain('type=model_api');
  });
});

describe('ProviderDetailView', () => {
  const routes = {
    'GET /api/v1/orgs/acme/model-providers/p-1': () => jsonResponse(PROVIDER),
  };
  const QUEUED = {
    id: 'j-1',
    provider_id: 'p-1',
    status: 'queued',
    result: null,
    error: null,
    created_at: '2026-05-02T10:00:00Z',
  };
  const queued = () => jsonResponse(QUEUED, { status: 202 });
  const done = (result: unknown) => () => jsonResponse({ ...QUEUED, status: 'done', result });

  it('runs a health check and shows status, latency and detail', async () => {
    stubInt({
      ...routes,
      'POST /api/v1/orgs/acme/model-providers/p-1/health-check': queued,
      'GET /api/v1/orgs/acme/model-providers/p-1/health-check-jobs/j-1': done({
        status: 'degraded',
        latency_ms: 950,
        checked_at: '2026-05-02T10:00:00Z',
        detail: 'slow responses',
      }),
    });
    renderWithClient(<ProviderDetailView id="p-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Run health check' }));
    const status = await screen.findByRole('status', undefined, { timeout: 5000 });
    expect(status).toHaveTextContent('degraded');
    expect(status).toHaveTextContent('950 ms');
    expect(status).toHaveTextContent('slow responses');
  });

  it('lists discovered models with a Register link that pre-fills the model form', async () => {
    stubInt({
      ...routes,
      'POST /api/v1/orgs/acme/model-providers/p-1/discover-models': queued,
      'GET /api/v1/orgs/acme/model-providers/p-1/discovery-jobs/j-1': done([
        {
          model_key: 'gpt-x',
          display_name: 'GPT X',
          context_window: 32000,
          capabilities: ['chat'],
        },
      ]),
    });
    renderWithClient(<ProviderDetailView id="p-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Discover models' }));
    const link = await screen.findByRole('link', { name: /Register/ }, { timeout: 5000 });
    const href = link.getAttribute('href') ?? '';
    expect(href).toContain('/o/acme/integrate/models?');
    const q = new URLSearchParams(href.split('?')[1]);
    expect(Object.fromEntries(q)).toMatchObject({
      register: '1',
      provider_id: 'p-1',
      model_key: 'gpt-x',
      display_name: 'GPT X',
      context_window: '32000',
      capabilities: 'chat',
    });
  });

  it('does not auto-register discovered models (no POST to /models)', async () => {
    const fetchMock = stubInt({
      ...routes,
      'POST /api/v1/orgs/acme/model-providers/p-1/discover-models': queued,
      'GET /api/v1/orgs/acme/model-providers/p-1/discovery-jobs/j-1': done([]),
    });
    renderWithClient(<ProviderDetailView id="p-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Discover models' }));
    expect(
      await screen.findByText('The provider reported no models.', undefined, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === 'POST /api/v1/orgs/acme/models')).toBe(false);
  });

  it('shows why a queued discovery failed', async () => {
    stubInt({
      ...routes,
      'POST /api/v1/orgs/acme/model-providers/p-1/discover-models': queued,
      'GET /api/v1/orgs/acme/model-providers/p-1/discovery-jobs/j-1': () =>
        jsonResponse({ ...QUEUED, status: 'failed', error: 'upstream_error' }),
    });
    renderWithClient(<ProviderDetailView id="p-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Discover models' }));
    expect(
      await screen.findByText('Discovery failed', undefined, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/The model discovery did not run \(upstream_error\)/),
    ).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Register/ })).toBeNull();
  });

  it('disables health check and discovery for a planned adapter', async () => {
    stubInt(
      {
        'GET /api/v1/orgs/acme/model-providers/p-1': () =>
          jsonResponse({ ...PROVIDER, adapter_key: 'databricks' }),
      },
      undefined,
      [ADAPTER_PLANNED],
    );
    renderWithClient(<ProviderDetailView id="p-1" />);
    expect(await screen.findByText(/declared only/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run health check' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Discover models' })).toBeDisabled();
  });
});
