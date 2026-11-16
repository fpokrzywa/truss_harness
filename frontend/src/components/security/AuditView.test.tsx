import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { list, stubApi } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { AuditView } from './AuditView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));

const EVENT = {
  id: 'e1',
  occurred_at: '2026-02-01T10:00:00Z',
  actor: 'alice@acme.test',
  action: 'apikey.create',
  target: 'ci-key',
  outcome: 'success',
  request_id: 'req-77',
};
const AUDIT = 'GET /api/v1/orgs/acme/audit-events';

afterEach(() => vi.unstubAllGlobals());

const lastParams = (fetchMock: ReturnType<typeof stubApi>) => {
  const calls = fetchMock.calls.filter((c) => c.key === AUDIT);
  return new URL(calls[calls.length - 1]?.url ?? '', 'http://x').searchParams;
};

describe('AuditView', () => {
  it('requests security events and renders them', async () => {
    const fetchMock = stubApi({ [AUDIT]: () => list([EVENT]) });
    renderWithClient(<AuditView />);
    expect(await screen.findByText('apikey.create')).toBeInTheDocument();
    expect(screen.getByText('alice@acme.test')).toBeInTheDocument();
    expect(lastParams(fetchMock).get('category')).toBe('security');
  });

  it('builds the query from the applied filters (ISO dates, empty filters omitted)', async () => {
    const fetchMock = stubApi({ [AUDIT]: () => list([]) });
    renderWithClient(<AuditView />);
    await screen.findByText(/No security events match/);

    await userEvent.type(screen.getByLabelText('Actor'), ' alice ');
    await userEvent.type(screen.getByLabelText('Action'), 'apikey.revoke');
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-01-02T03:04' } });
    await userEvent.click(screen.getByRole('button', { name: 'Apply filters' }));

    await waitFor(() => expect(lastParams(fetchMock).get('actor')).toBe('alice'));
    const params = lastParams(fetchMock);
    expect(params.get('category')).toBe('security');
    expect(params.get('action')).toBe('apikey.revoke');
    expect(params.get('from')).toBe(new Date('2026-01-02T03:04').toISOString());
    expect(params.has('to')).toBe(false);
    expect(params.has('cursor')).toBe(false);

    await userEvent.click(screen.getByRole('button', { name: 'Clear' }));
    await waitFor(() => expect(lastParams(fetchMock).has('actor')).toBe(false));
  });

  it('pages with the cursor via Load more', async () => {
    const fetchMock = stubApi({
      [AUDIT]: ({ url }) =>
        new URL(url, 'http://x').searchParams.get('cursor') === 'c1'
          ? list([{ ...EVENT, id: 'e2', action: 'secret.rotate' }])
          : list([EVENT], 'c1'),
    });
    renderWithClient(<AuditView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('secret.rotate')).toBeInTheDocument();
    expect(screen.getByText('apikey.create')).toBeInTheDocument();
    expect(lastParams(fetchMock).get('cursor')).toBe('c1');
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
  });

  it('shows the chain verification result', async () => {
    stubApi({
      [AUDIT]: () => list([]),
      'GET /api/v1/orgs/acme/audit-events/verify': () => jsonResponse({ ok: true, checked: 42 }),
    });
    renderWithClient(<AuditView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Verify chain' }));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Chain verified: 42 events checked, no tampering detected.',
    );
  });

  it('reports a broken chain with the first broken event', async () => {
    stubApi({
      [AUDIT]: () => list([]),
      'GET /api/v1/orgs/acme/audit-events/verify': () =>
        jsonResponse({ ok: false, checked: 10, first_broken_id: 'evt-9' }),
    });
    renderWithClient(<AuditView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Verify chain' }));
    const status = await screen.findByText(/verification FAILED/);
    expect(status).toHaveTextContent('First broken event: evt-9');
  });

  it('renders the neutral no-access view on 403', async () => {
    stubApi({ [AUDIT]: () => problemResponse(403, 'forbidden') });
    renderWithClient(<AuditView />);
    expect(await screen.findByText('Not found or no access')).toBeInTheDocument();
  });
});
