import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { list, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { AuditView } from './AuditView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const AUDIT = 'GET /api/v1/orgs/acme/audit-events';
const EVENT = {
  id: 'e1',
  occurred_at: '2026-11-01T10:00:00Z',
  actor: 'alice@acme.test',
  action: 'budget.update',
  category: 'governance',
  target: 'b1',
  outcome: 'success',
  request_id: 'req-1',
};
const params = (fetchMock: ReturnType<typeof stubApi>) => {
  const c = fetchMock.calls.filter((x) => x.key === AUDIT).at(-1);
  return new URL(c?.url ?? '', 'http://x').searchParams;
};

describe('Audit Explorer (AuditView explorer mode)', () => {
  it('reads all categories by default, shows the category column and filters by category', async () => {
    const fetchMock = stubApi({ [AUDIT]: () => list([EVENT]) });
    renderWithClient(<AuditView explorer />);
    expect(await screen.findByText('budget.update')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Audit explorer' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Category' })).toBeInTheDocument();
    expect(
      within(screen.getByRole('table', { name: 'Audit events' })).getByText('governance'),
    ).toBeInTheDocument();
    expect(params(fetchMock).has('category')).toBe(false); // not limited to security

    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText('Category'), 'governance');
    await user.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(params(fetchMock).get('category')).toBe('governance'));

    await user.click(screen.getByRole('button', { name: 'Clear' }));
    await waitFor(() => expect(params(fetchMock).has('category')).toBe(false));
  });

  it('verifies the hash chain like the security audit view', async () => {
    stubApi({
      [AUDIT]: () => list([]),
      'GET /api/v1/orgs/acme/audit-events/verify': () => jsonResponse({ ok: true, checked: 7 }),
    });
    renderWithClient(<AuditView explorer />);
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Verify chain' }));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Chain verified: 7 events checked, no tampering detected.',
    );
  });

  it('leaves the security audit page unchanged (category=security, no category filter)', async () => {
    const fetchMock = stubApi({ [AUDIT]: () => list([EVENT]) });
    renderWithClient(<AuditView />);
    await screen.findByText('budget.update');
    expect(params(fetchMock).get('category')).toBe('security');
    expect(screen.queryByLabelText('Category')).not.toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Category' })).not.toBeInTheDocument();
  });
});
