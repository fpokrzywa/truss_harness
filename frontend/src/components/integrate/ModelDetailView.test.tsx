import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ADAPTER_PLANNED, stubInt } from '@/test/integrate';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { ModelDetailView } from './ModelDetailView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const MODEL = {
  id: 'm-1',
  provider_id: 'p-1',
  model_key: 'fake-1',
  display_name: 'Fake One',
  capabilities: ['chat', 'tools'],
  context_window: 8000,
  max_output_tokens: 1000,
  lifecycle: 'active',
  rate_limits: { rpm: 60 },
  row_version: 4,
};
const routes = (provider = 'fake') => ({
  'GET /api/v1/orgs/acme/models/m-1': () => jsonResponse(MODEL),
  'GET /api/v1/orgs/acme/model-providers': () =>
    list([{ id: 'p-1', name: 'Local', adapter_key: provider, connection_id: 'c-1' }]),
});

describe('ModelDetailView', () => {
  it('shows model facts', async () => {
    stubInt(routes());
    renderWithClient(<ModelDetailView id="m-1" />);
    expect(await screen.findByRole('heading', { name: 'Fake One' })).toBeInTheDocument();
    expect(screen.getByText('chat, tools')).toBeInTheDocument();
    expect(screen.getByText('rpm: 60')).toBeInTheDocument();
  });

  it('invokes the model and renders output, usage and latency', async () => {
    const fetchMock = stubInt({
      ...routes(),
      'POST /api/v1/orgs/acme/models/m-1/invoke': () =>
        jsonResponse({
          output: 'Hello there',
          usage: { input_tokens: 11, output_tokens: 22 },
          latency_ms: 87,
          provider_request_id: 'req-abc',
        }),
    });
    renderWithClient(<ModelDetailView id="m-1" />);
    await userEvent.type(await screen.findByLabelText('Message 1'), 'Say hi');
    await userEvent.type(screen.getByLabelText('Max tokens'), '50');
    await userEvent.click(screen.getByRole('button', { name: 'Invoke model' }));
    const result = await screen.findByRole('region', { name: 'Invocation result' });
    expect(result).toHaveTextContent('Hello there');
    expect(result).toHaveTextContent('Input tokens');
    expect(result).toHaveTextContent('11');
    expect(result).toHaveTextContent('22');
    expect(result).toHaveTextContent('87 ms');
    expect(result).toHaveTextContent('req-abc');
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/models/m-1/invoke')).toEqual({
      messages: [{ role: 'user', content: 'Say hi' }],
      max_tokens: 50,
    });
  });

  it('sends multiple messages with roles', async () => {
    const fetchMock = stubInt({
      ...routes(),
      'POST /api/v1/orgs/acme/models/m-1/invoke': () =>
        jsonResponse({ output: 'ok', usage: { input_tokens: 1, output_tokens: 1 }, latency_ms: 1 }),
    });
    renderWithClient(<ModelDetailView id="m-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Add message' }));
    await userEvent.selectOptions(screen.getByLabelText('Role of message 1'), 'system');
    await userEvent.type(screen.getByLabelText('Message 1'), 'Be brief');
    await userEvent.type(screen.getByLabelText('Message 2'), 'Hi');
    await userEvent.click(screen.getByRole('button', { name: 'Invoke model' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/models/m-1/invoke')).toEqual({
        messages: [
          { role: 'system', content: 'Be brief' },
          { role: 'user', content: 'Hi' },
        ],
      }),
    );
  });

  it('requires a message and shows rate-limit errors from the server', async () => {
    const fetchMock = stubInt({
      ...routes(),
      'POST /api/v1/orgs/acme/models/m-1/invoke': () =>
        problemResponse(429, 'model_rate_limited', { detail: 'Rate limit reached.' }),
    });
    renderWithClient(<ModelDetailView id="m-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Invoke model' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter at least one message.');
    expect(fetchMock.calls.some((c) => c.key.endsWith('/invoke'))).toBe(false);
    await userEvent.type(screen.getByLabelText('Message 1'), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Invoke model' }));
    expect(await screen.findByText('Rate limit reached.')).toBeInTheDocument();
    expect(screen.getByText('model_rate_limited')).toBeInTheDocument();
  });

  it('disables invocation with an explanation when the provider adapter is planned', async () => {
    stubInt(routes('databricks'), undefined, [ADAPTER_PLANNED]);
    renderWithClient(<ModelDetailView id="m-1" />);
    const button = await screen.findByRole('button', { name: 'Invoke model' });
    await waitFor(() => expect(button).toBeDisabled());
    expect(screen.getByText(/declared only/)).toBeInTheDocument();
    expect(screen.getByLabelText('Message 1')).toBeDisabled();
  });

  it('hides the console without model:invoke', async () => {
    stubInt(routes(), ['model:read']);
    renderWithClient(<ModelDetailView id="m-1" />);
    await screen.findByRole('heading', { name: 'Fake One' });
    expect(screen.queryByRole('button', { name: 'Invoke model' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Lifecycle' })).not.toBeInTheDocument();
  });

  it('transitions lifecycle with row_version, confirming retirement, and offers none once retired', async () => {
    const fetchMock = stubInt({
      ...routes(),
      'PATCH /api/v1/orgs/acme/models/m-1': () =>
        jsonResponse({ ...MODEL, lifecycle: 'deprecated', row_version: 5 }),
    });
    const { unmount } = renderWithClient(<ModelDetailView id="m-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Deprecate' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'PATCH /api/v1/orgs/acme/models/m-1')).toEqual({
        lifecycle: 'deprecated',
        row_version: 4,
      }),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Retire' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Retirement cannot be undone');
    unmount();

    stubInt({
      'GET /api/v1/orgs/acme/models/m-1': () => jsonResponse({ ...MODEL, lifecycle: 'retired' }),
      'GET /api/v1/orgs/acme/model-providers': () => list([]),
    });
    renderWithClient(<ModelDetailView id="m-1" />);
    expect(await screen.findByText(/Retirement is terminal/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Invoke model' })).toBeDisabled();
  });
});
