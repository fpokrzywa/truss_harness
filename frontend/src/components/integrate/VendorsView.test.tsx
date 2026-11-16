import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { stubInt } from '@/test/integrate';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { VendorDetailView } from './VendorDetailView';
import { VendorsView } from './VendorsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const GLOBAL = {
  id: 'v-dbx',
  key: 'databricks',
  name: 'Databricks',
  category: 'data_platform',
  status: 'active',
  is_global: true,
  row_version: 1,
};
const PRIVATE = {
  id: 'v-own',
  key: 'acme-erp',
  name: 'Acme ERP',
  category: 'saas_api',
  status: 'active',
  is_global: false,
  row_version: 7,
};

describe('VendorsView', () => {
  it('lists global and private vendors; global entries are read-only', async () => {
    stubInt({ 'GET /api/v1/orgs/acme/vendors': () => list([GLOBAL, PRIVATE]) });
    renderWithClient(<VendorsView />);
    expect(await screen.findByRole('link', { name: 'Databricks' })).toBeInTheDocument();
    const dbx = screen.getByRole('cell', { name: 'Databricks' }).closest('tr')!;
    const own = screen.getByRole('cell', { name: 'Acme ERP' }).closest('tr')!;
    expect(within(dbx).getByText('Global')).toBeInTheDocument();
    expect(within(dbx).queryByRole('button', { name: /Edit/ })).not.toBeInTheDocument();
    expect(within(own).getByRole('button', { name: /Edit/ })).toBeInTheDocument();
  });

  it('passes category and status filters to the server', async () => {
    const fetchMock = stubInt({ 'GET /api/v1/orgs/acme/vendors': () => list([]) });
    renderWithClient(<VendorsView />);
    await userEvent.selectOptions(await screen.findByLabelText('Category'), 'mcp');
    await userEvent.selectOptions(screen.getByLabelText('Status'), 'disabled');
    await waitFor(() => {
      const last = fetchMock.calls.filter((c) => c.key === 'GET /api/v1/orgs/acme/vendors').pop();
      expect(last?.url).toContain('category=mcp');
      expect(last?.url).toContain('status=disabled');
    });
  });

  it('creates a private vendor after validating the key', async () => {
    const fetchMock = stubInt({
      'GET /api/v1/orgs/acme/vendors': () => list([]),
      'POST /api/v1/orgs/acme/vendors': () => jsonResponse(PRIVATE, { status: 201 }),
    });
    renderWithClient(<VendorsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create private vendor' }));
    const dlg = screen.getByRole('dialog');
    await userEvent.type(within(dlg).getByLabelText('Key'), 'Bad Key');
    await userEvent.type(within(dlg).getByLabelText('Name'), 'Acme ERP');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Save vendor' }));
    expect(within(dlg).getByText(/Use lowercase letters/)).toBeInTheDocument();
    await userEvent.clear(within(dlg).getByLabelText('Key'));
    await userEvent.type(within(dlg).getByLabelText('Key'), 'acme-erp');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Save vendor' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/vendors')).toEqual({
      key: 'acme-erp',
      name: 'Acme ERP',
      category: 'saas_api',
    });
  });

  it('confirms a status transition and sends the row_version', async () => {
    const fetchMock = stubInt({
      'GET /api/v1/orgs/acme/vendors': () => list([PRIVATE]),
      'POST /api/v1/orgs/acme/vendors/v-own/status': () =>
        jsonResponse({ ...PRIVATE, status: 'retired', row_version: 8 }),
    });
    renderWithClient(<VendorsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Retire/ }));
    expect(fetchMock.calls.some((c) => c.key.endsWith('/status'))).toBe(false);
    const dlg = screen.getByRole('dialog');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Retire vendor' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/vendors/v-own/status')).toEqual({
        status: 'retired',
        row_version: 7,
      }),
    );
  });

  it('hides create and edit without vendor:manage', async () => {
    stubInt({ 'GET /api/v1/orgs/acme/vendors': () => list([PRIVATE]) }, ['vendor:read']);
    renderWithClient(<VendorsView />);
    await screen.findByRole('link', { name: 'Acme ERP' });
    expect(screen.queryByRole('button', { name: /Create private vendor/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Edit/ })).not.toBeInTheDocument();
  });
});

describe('VendorDetailView capability matrix (honesty)', () => {
  const routes = {
    'GET /api/v1/orgs/acme/vendors/v-dbx': () => jsonResponse(GLOBAL),
    'GET /api/v1/orgs/acme/vendors/v-dbx/capabilities': () =>
      list([
        { capability: 'query', adapter_key: null, maturity: 'planned' },
        { capability: 'discover', adapter_key: null, maturity: 'planned' },
      ]),
  };

  it('renders planned capabilities as "Declared — not validated", never as working', async () => {
    stubInt(routes);
    renderWithClient(<VendorDetailView id="v-dbx" />);
    const table = await screen.findByRole('table', { name: 'Capability matrix' });
    expect(within(table).getAllByText('Declared — not validated')).toHaveLength(2);
    expect(within(table).queryByText('Validated')).not.toBeInTheDocument();
    expect(within(table).queryByText('Experimental')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /test|invoke/i })).not.toBeInTheDocument();
  });

  it('shows validated capabilities with suite version and evidence link, experimental without', async () => {
    stubInt({
      ...routes,
      'GET /api/v1/orgs/acme/vendors/v-dbx/capabilities': () =>
        list([
          {
            capability: 'invoke',
            adapter_key: 'rest.http',
            maturity: 'validated',
            evidence: { run_id: 'r1', finished_at: '2026-05-01T10:00:00Z', suite_version: '2.1' },
          },
          { capability: 'stream', adapter_key: 'rest.http', maturity: 'experimental' },
        ]),
    });
    renderWithClient(<VendorDetailView id="v-dbx" />);
    const table = await screen.findByRole('table', { name: 'Capability matrix' });
    expect(within(table).getByText('Validated')).toBeInTheDocument();
    expect(within(table).getByRole('link', { name: /Suite 2\.1/ })).toHaveAttribute(
      'href',
      '/o/acme/integrate/adapters#rest.http',
    );
    expect(within(table).getByText('Experimental')).toBeInTheDocument();
    expect(within(table).getByText('None recorded')).toBeInTheDocument();
  });

  it('accepts a bare array response for capabilities', async () => {
    stubInt({
      ...routes,
      'GET /api/v1/orgs/acme/vendors/v-dbx/capabilities': () =>
        jsonResponse([{ capability: 'query', adapter_key: null, maturity: 'planned' }]),
    });
    renderWithClient(<VendorDetailView id="v-dbx" />);
    expect(await screen.findByText('Declared — not validated')).toBeInTheDocument();
  });
});
