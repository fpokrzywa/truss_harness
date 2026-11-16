import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { stubInt } from '@/test/integrate';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { ModelsView } from './ModelsView';

let search = '';
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(search),
}));
beforeEach(() => {
  search = '';
});
afterEach(() => vi.unstubAllGlobals());

const model = (id: string, name: string) => ({
  id,
  provider_id: 'p-1',
  model_key: `${id}-key`,
  display_name: name,
  capabilities: ['chat'],
  context_window: 8000,
  max_output_tokens: 1000,
  lifecycle: 'active',
  row_version: 1,
});
const MODELS = [model('m-a', 'Model A'), model('m-b', 'Model B'), model('m-c', 'Model C')];

const base = {
  'GET /api/v1/orgs/acme/models': () => list(MODELS),
  'GET /api/v1/orgs/acme/models/effective': () => list([MODELS[0]]),
  'GET /api/v1/orgs/acme/model-providers': () =>
    list([{ id: 'p-1', name: 'Local fake', adapter_key: 'fake', connection_id: 'c-1' }]),
  'GET /api/v1/orgs/acme/model-routes': () => list([]),
  'GET /api/v1/orgs/acme/model-allowlists': () => jsonResponse({ enabled: false, model_ids: [] }),
};

describe('ModelsView', () => {
  it('lists models with provider, capabilities and lifecycle', async () => {
    stubInt(base);
    renderWithClient(<ModelsView />);
    expect(await screen.findByRole('link', { name: 'Model A' })).toHaveAttribute(
      'href',
      '/o/acme/integrate/models/m-a',
    );
    expect(screen.getAllByText('Local fake').length).toBeGreaterThan(0);
  });

  it('shows effective models for a scope', async () => {
    stubInt(base);
    renderWithClient(<ModelsView />);
    const list_ = await screen.findByRole('list', { name: 'Effective models' });
    expect(within(list_).getByText('Model A')).toBeInTheDocument();
    expect(within(list_).queryByText('Model B')).not.toBeInTheDocument();
  });

  it('saves an allowlist for the tenant scope with the enabled flag and model ids', async () => {
    const fetchMock = stubInt({
      ...base,
      'PUT /api/v1/orgs/acme/model-allowlists': () =>
        jsonResponse({ enabled: true, model_ids: ['m-a'] }),
    });
    renderWithClient(<ModelsView />);
    const enforce = await screen.findByLabelText('Enforce allowlist at this scope');
    expect(screen.getByLabelText('Model A', { selector: 'fieldset input' })).toBeDisabled();
    await userEvent.click(enforce);
    expect(screen.getByText(/every model is denied at this scope/)).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText('Model A', { selector: 'fieldset input' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save allowlist' }));
    await waitFor(() => expect(screen.getByText('Allowlist saved.')).toBeInTheDocument());
    expect(bodyOf(fetchMock, 'PUT /api/v1/orgs/acme/model-allowlists')).toEqual({
      enabled: true,
      model_ids: ['m-a'],
    });
    const put = fetchMock.calls.find((c) => c.key.startsWith('PUT'));
    expect(put?.url).toContain('scope_type=tenant');
    expect(put?.url).toContain('scope_id=org-1');
  });

  it('hides allowlist saving without model:manage', async () => {
    stubInt(base, ['model:read']);
    renderWithClient(<ModelsView />);
    await screen.findByLabelText('Enforce allowlist at this scope');
    expect(screen.queryByRole('button', { name: 'Save allowlist' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Register model' })).not.toBeInTheDocument();
  });

  it('keeps fallback order as edited and posts primary + ordered fallbacks', async () => {
    const fetchMock = stubInt({
      ...base,
      'POST /api/v1/orgs/acme/model-routes': () => jsonResponse({ id: 'r-1' }, { status: 201 }),
    });
    renderWithClient(<ModelsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create route' }));
    const dlg = screen.getByRole('dialog');
    await userEvent.type(within(dlg).getByLabelText('Name'), 'default');
    await userEvent.selectOptions(within(dlg).getByLabelText('Primary model'), 'm-a');
    await userEvent.selectOptions(within(dlg).getByLabelText('Add fallback'), 'm-b');
    await userEvent.selectOptions(within(dlg).getByLabelText('Add fallback'), 'm-c');
    let order = within(within(dlg).getByRole('list', { name: 'Fallback order' }))
      .getAllByRole('listitem')
      .map((li) => li.textContent);
    expect(order[0]).toContain('Model B');
    expect(order[1]).toContain('Model C');

    await userEvent.click(within(dlg).getByRole('button', { name: /Move up Model C/ }));
    order = within(within(dlg).getByRole('list', { name: 'Fallback order' }))
      .getAllByRole('listitem')
      .map((li) => li.textContent);
    expect(order[0]).toContain('Model C');
    expect(within(dlg).getByRole('button', { name: /Move up Model C/ })).toBeDisabled();

    await userEvent.click(within(dlg).getByRole('button', { name: 'Save route' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/model-routes')).toEqual({
      name: 'default',
      scope: { type: 'tenant', id: 'org-1' },
      primary_model_id: 'm-a',
      fallback_model_ids: ['m-c', 'm-b'],
    });
  });

  it('does not offer the primary model as a fallback, and drops it if chosen as primary later', async () => {
    stubInt(base);
    renderWithClient(<ModelsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create route' }));
    const dlg = screen.getByRole('dialog');
    await userEvent.selectOptions(within(dlg).getByLabelText('Primary model'), 'm-a');
    await userEvent.selectOptions(within(dlg).getByLabelText('Add fallback'), 'm-b');
    expect(
      within(within(dlg).getByLabelText('Add fallback')).queryByRole('option', { name: 'Model A' }),
    ).toBeNull();
    await userEvent.selectOptions(within(dlg).getByLabelText('Primary model'), 'm-b');
    expect(within(dlg).getByText('No fallbacks.')).toBeInTheDocument();
  });

  it('shows API validation errors for a route (model not allowed at scope)', async () => {
    stubInt({
      ...base,
      'POST /api/v1/orgs/acme/model-routes': () =>
        problemResponse(422, 'model_not_allowed', {
          detail: 'Model B is not allowed at this scope.',
        }),
    });
    renderWithClient(<ModelsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create route' }));
    const dlg = screen.getByRole('dialog');
    await userEvent.type(within(dlg).getByLabelText('Name'), 'r');
    await userEvent.selectOptions(within(dlg).getByLabelText('Primary model'), 'm-a');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Save route' }));
    expect(
      await within(dlg).findByText('Model B is not allowed at this scope.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('registers a model with capabilities and limits', async () => {
    const fetchMock = stubInt({
      ...base,
      'POST /api/v1/orgs/acme/models': () => jsonResponse(MODELS[0], { status: 201 }),
    });
    renderWithClient(<ModelsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Register model' }));
    const dlg = screen.getByRole('dialog');
    await userEvent.selectOptions(within(dlg).getByLabelText('Provider'), 'p-1');
    await userEvent.type(within(dlg).getByLabelText('Model key'), 'fake-1');
    await userEvent.type(within(dlg).getByLabelText('Display name'), 'Fake One');
    await userEvent.click(within(dlg).getByLabelText('vision'));
    await userEvent.type(within(dlg).getByLabelText('Context window (tokens)'), '8000');
    await userEvent.type(within(dlg).getByLabelText('Max output tokens'), '9000');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Register model' }));
    expect(within(dlg).getByText('Cannot exceed the context window.')).toBeInTheDocument();
    await userEvent.clear(within(dlg).getByLabelText('Max output tokens'));
    await userEvent.type(within(dlg).getByLabelText('Max output tokens'), '1000');
    await userEvent.type(within(dlg).getByLabelText('Requests per minute'), '60');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Register model' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/models')).toEqual({
      provider_id: 'p-1',
      model_key: 'fake-1',
      display_name: 'Fake One',
      capabilities: ['chat', 'vision'],
      context_window: 8000,
      max_output_tokens: 1000,
      lifecycle: 'preview',
      rate_limits: { rpm: 60 },
    });
  });

  it('opens the register dialog pre-filled from a discovered model', async () => {
    search =
      'register=1&provider_id=p-1&model_key=gpt-x&display_name=GPT%20X&context_window=32000&max_output_tokens=4096&capabilities=chat,tools,bogus';
    stubInt(base);
    renderWithClient(<ModelsView />);
    const dlg = await screen.findByRole('dialog');
    expect(within(dlg).getByLabelText('Model key')).toHaveValue('gpt-x');
    expect(within(dlg).getByLabelText('Display name')).toHaveValue('GPT X');
    expect(within(dlg).getByLabelText('Context window (tokens)')).toHaveValue(32000);
    expect(within(dlg).getByLabelText('tools')).toBeChecked();
    expect(within(dlg).getByLabelText('vision')).not.toBeChecked();
    await waitFor(() => expect(within(dlg).getByLabelText('Provider')).toHaveValue('p-1'));
  });
});
