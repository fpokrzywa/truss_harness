import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { APPGEN_PERMS, AR, connection, ORG, request, stubAppgen, SUMMARY } from '@/test/appgen';
import { ALL_PERMS, bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { GenerateView } from './GenerateView';

const nav = vi.hoisted(() => ({ query: 'ws=eng&proj=api', push: vi.fn() }));
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(nav.query),
  usePathname: () => '/o/acme/solutions/generate',
  useRouter: () => ({ push: nav.push, replace: vi.fn() }),
}));
beforeEach(() => {
  nav.push.mockReset();
  nav.query = 'ws=eng&proj=api';
});
afterEach(() => vi.unstubAllGlobals());

const CREATE = `POST ${AR}`;

async function fillForm() {
  const form = await screen.findByRole('form', { name: 'New app request' });
  await userEvent.selectOptions(within(form).getByLabelText('Connection'), 'conn-dbx');
  await userEvent.click(await within(form).findByLabelText('Main.Sales.Orders'));
  await userEvent.type(within(form).getByLabelText('Brief'), 'Sales by region');
  await userEvent.selectOptions(within(form).getByLabelText('Drafting model'), 'm-1');
  return form;
}

describe('GenerateView new request', () => {
  it('asks for a project before anything else', async () => {
    nav.query = '';
    const fetchMock = stubAppgen();
    renderWithClient(<GenerateView />);
    expect(await screen.findByText(/Choose a workspace and a project/)).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.includes('app-requests'))).toBe(false);
  });

  it('offers only active, read-only Databricks connections of the project', async () => {
    const fetchMock = stubAppgen({
      [`GET ${ORG}/connections`]: () =>
        list([
          connection(),
          connection({ id: 'c-rw', name: 'Writer', access_mode: 'read_write' }),
          connection({ id: 'c-sf', name: 'Snow', adapter_key: 'snowflake' }),
          connection({ id: 'c-off', name: 'Off', status: 'disabled' }),
        ]),
    });
    renderWithClient(<GenerateView />);
    const select = await screen.findByLabelText('Connection');
    const options = within(select)
      .getAllByRole('option')
      .map((o) => o.textContent);
    expect(options).toEqual(['Select...', 'Sales warehouse']);
    const url = fetchMock.calls.find((c) => c.key === `GET ${ORG}/connections`)!.url;
    expect(url).toContain('workspace=eng');
    expect(url).toContain('project=api');
    expect(url).toContain('include_shared=true');
  });

  it('shows the empty state when no connection is eligible', async () => {
    stubAppgen({
      [`GET ${ORG}/connections`]: () =>
        list([
          connection({ id: 'c-rw', access_mode: 'read_write' }),
          connection({ id: 'c-sf', adapter_key: 'snowflake' }),
          connection({ id: 'c-off', status: 'disabled' }),
        ]),
    });
    renderWithClient(<GenerateView />);
    expect(
      await screen.findByText(/needs an active, read-only Databricks connection/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText('Connection')).not.toBeInTheDocument();
  });

  it('shows the loading text, then a 500 error of the connection list', async () => {
    stubAppgen({ [`GET ${ORG}/connections`]: () => problemResponse(500, 'internal_error') });
    renderWithClient(<GenerateView />);
    expect(await screen.findByText('Loading connections...')).toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('internal_error');
  });

  it('shows the no-access view when the connection list is refused', async () => {
    stubAppgen({ [`GET ${ORG}/connections`]: () => problemResponse(403, 'forbidden') });
    renderWithClient(<GenerateView />);
    expect(
      await screen.findByRole('heading', { name: 'Not found or no access' }),
    ).toBeInTheDocument();
  });

  it('offers the discovered tables and submits the lowercased names', async () => {
    const fetchMock = stubAppgen({
      [CREATE]: () => jsonResponse(request({ id: 'ar-9', status: 'queued' }), { status: 202 }),
    });
    renderWithClient(<GenerateView />);
    const form = await fillForm();
    expect(within(form).getByLabelText('main.sales.customers')).toBeInTheDocument();
    await userEvent.click(within(form).getByRole('button', { name: 'Generate draft' }));
    await waitFor(() =>
      expect(nav.push).toHaveBeenCalledWith('/o/acme/solutions/generate/ar-9?ws=eng&proj=api'),
    );
    expect(bodyOf(fetchMock, CREATE)).toEqual({
      connection_id: 'conn-dbx',
      tables: ['main.sales.orders'],
      brief: 'Sales by region',
      model_id: 'm-1',
    });
  });

  it('blocks the submit without a connection, tables, a brief or a model', async () => {
    const fetchMock = stubAppgen();
    renderWithClient(<GenerateView />);
    const form = await screen.findByRole('form', { name: 'New app request' });
    await userEvent.click(within(form).getByRole('button', { name: 'Generate draft' }));
    expect(within(form).getByText('Choose a connection.')).toBeInTheDocument();
    expect(within(form).getByText('The brief must be 1 to 4000 characters.')).toBeInTheDocument();
    expect(within(form).getByText('Choose a drafting model.')).toBeInTheDocument();
    await userEvent.selectOptions(within(form).getByLabelText('Connection'), 'conn-dbx');
    await within(form).findByLabelText('Main.Sales.Orders');
    await userEvent.click(within(form).getByRole('button', { name: 'Generate draft' }));
    expect(within(form).getByText('Choose at least one table.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === CREATE)).toBe(false);
  });

  it('shows a 422 table_not_discovered and keeps the inputs', async () => {
    stubAppgen({
      [CREATE]: () =>
        problemResponse(422, 'table_not_discovered', { detail: 'main.sales.orders is gone' }),
    });
    renderWithClient(<GenerateView />);
    const form = await fillForm();
    await userEvent.click(within(form).getByRole('button', { name: 'Generate draft' }));
    const alert = await within(form).findByRole('alert');
    expect(alert).toHaveTextContent('Could not start the draft');
    expect(alert).toHaveTextContent('table_not_discovered');
    expect(within(form).getByLabelText('Brief')).toHaveValue('Sales by region');
    expect(within(form).getByLabelText('Main.Sales.Orders')).toBeChecked();
    expect(nav.push).not.toHaveBeenCalled();
  });

  it('shows a 403 on create and keeps the inputs', async () => {
    stubAppgen({ [CREATE]: () => problemResponse(403, 'forbidden') });
    renderWithClient(<GenerateView />);
    const form = await fillForm();
    await userEvent.click(within(form).getByRole('button', { name: 'Generate draft' }));
    expect(await within(form).findByRole('alert')).toHaveTextContent('forbidden');
    expect(within(form).getByLabelText('Drafting model')).toHaveValue('m-1');
  });

  it('says the connection has no discovered tables on a 404 latest discovery', async () => {
    stubAppgen({
      [`GET ${ORG}/connections/conn-dbx/discoveries/latest`]: () =>
        problemResponse(404, 'not_found'),
    });
    renderWithClient(<GenerateView />);
    await userEvent.selectOptions(await screen.findByLabelText('Connection'), 'conn-dbx');
    expect(
      await screen.findByText(/This connection has no discovered tables; run a discovery first/),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'connection' })).toHaveAttribute(
      'href',
      '/o/acme/connections/conn-dbx',
    );
  });

  it('says the same for a discovery without tables', async () => {
    stubAppgen({
      [`GET ${ORG}/connections/conn-dbx/discoveries/latest`]: () =>
        jsonResponse({ kind: 'schema', payload: { tables: [] } }),
    });
    renderWithClient(<GenerateView />);
    await userEvent.selectOptions(await screen.findByLabelText('Connection'), 'conn-dbx');
    expect(await screen.findByText(/no discovered tables/)).toBeInTheDocument();
  });

  it('has no New request tab without appgen:request:write', async () => {
    stubAppgen({}, [...ALL_PERMS, 'appgen:request:read']);
    renderWithClient(<GenerateView />);
    expect(await screen.findByText(/can view this project's app requests but not create/));
    expect(screen.queryByRole('tab', { name: 'New request' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Requests' })).toHaveAttribute('aria-selected', 'true');
  });
});

describe('GenerateView requests', () => {
  async function openRequests() {
    await userEvent.click(await screen.findByRole('tab', { name: 'Requests' }));
    return screen.getByRole('tabpanel', { name: 'Requests' });
  }

  it('lists the requests with links and loads more with the cursor', async () => {
    const fetchMock = stubAppgen({
      [`GET ${AR}`]: ({ url }) =>
        url.includes('cursor=cur-2')
          ? list([{ ...SUMMARY, id: 'ar-2', status: 'failed', status_reason: 'model timed out' }])
          : list([SUMMARY], 'cur-2'),
    });
    renderWithClient(<GenerateView />);
    const panel = await openRequests();
    const table = await within(panel).findByRole('table', { name: 'App requests' });
    expect(within(table).getByText('drafted')).toBeInTheDocument();
    expect(within(table).getByText('r1')).toBeInTheDocument();
    expect(within(table).getByRole('link', { name: /Open/ })).toHaveAttribute(
      'href',
      '/o/acme/solutions/generate/ar-1?ws=eng&proj=api',
    );
    await userEvent.click(within(panel).getByRole('button', { name: 'Load more' }));
    expect(await within(table).findByText('model timed out')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.url.includes('cursor=cur-2'))).toBe(true);
    expect(within(panel).queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
  });

  it('sends the chosen status', async () => {
    const fetchMock = stubAppgen();
    renderWithClient(<GenerateView />);
    const panel = await openRequests();
    await within(panel).findByRole('table', { name: 'App requests' });
    await userEvent.selectOptions(within(panel).getByLabelText('Status'), 'failed');
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.url.includes('status=failed'))).toBe(true),
    );
  });

  it('shows the empty state for an empty page', async () => {
    stubAppgen({ [`GET ${AR}`]: () => list([]) });
    renderWithClient(<GenerateView />);
    const panel = await openRequests();
    expect(
      await within(panel).findByText('No app requests in this project yet.'),
    ).toBeInTheDocument();
  });

  it('shows the loading text and a 500 error', async () => {
    stubAppgen({ [`GET ${AR}`]: () => problemResponse(500, 'internal_error') });
    renderWithClient(<GenerateView />);
    const panel = await openRequests();
    expect(await within(panel).findByRole('alert')).toHaveTextContent('internal_error');
  });

  it('shows the no-access view for 403 and 404', async () => {
    for (const status of [403, 404]) {
      stubAppgen({ [`GET ${AR}`]: () => problemResponse(status, 'forbidden') }, [
        ...ALL_PERMS,
        ...APPGEN_PERMS,
      ]);
      const { unmount } = renderWithClient(<GenerateView />);
      const panel = await openRequests();
      expect(
        await within(panel).findByRole('heading', { name: 'Not found or no access' }),
      ).toBeInTheDocument();
      unmount();
    }
  });
});
