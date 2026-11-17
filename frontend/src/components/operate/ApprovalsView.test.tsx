import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { approval, run, stubOperate } from '@/test/operate';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { ApprovalsView } from './ApprovalsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const A = '/api/v1/orgs/acme/approvals';

function setup(a = approval(), extra: Record<string, () => Response> = {}) {
  return stubOperate({
    [`GET ${A}`]: () => list([a]),
    [`GET ${A}/${a.id}`]: () => jsonResponse(a),
    ...extra,
  });
}

async function open() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: /Review approval ap-1/ }));
  await screen.findByRole('region', { name: 'Approval detail' });
  return user;
}

describe('ApprovalsView', () => {
  it('shows the exact arguments, tool identity, requester and expiry', async () => {
    setup();
    renderWithClient(<ApprovalsView />);
    await open();
    const args = screen.getByLabelText('Exact arguments');
    expect(JSON.parse(args.textContent ?? '')).toEqual({
      customer_id: 'c-9',
      patch: { tier: 'gold' },
    });
    const detail = screen.getByRole('region', { name: 'Approval detail' });
    expect(detail).toHaveTextContent('crm.update');
    expect(detail).toHaveTextContent('version 3');
    expect(detail).toHaveTextContent('write_non_idempotent');
    expect(detail).toHaveTextContent('Olive');
    expect(within(detail).getByRole('timer')).toHaveTextContent(/left/);
  });

  it('hides decision buttons when the current user started the run', async () => {
    setup(approval({ run_initiated_by: { type: 'user', id: 'u-me', display: 'Me' } }));
    renderWithClient(<ApprovalsView />);
    await open();
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent('someone else must decide');
  });

  it('approves with a comment', async () => {
    const fetchMock = setup(approval(), {
      [`POST ${A}/ap-1/decide`]: () => jsonResponse(approval({ status: 'approved' })),
    });
    renderWithClient(<ApprovalsView />);
    const user = await open();
    await user.type(screen.getByLabelText('Comment (optional)'), 'looks right');
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, `POST ${A}/ap-1/decide`)).toEqual({
        decision: 'approve',
        comment: 'looks right',
      }),
    );
  });

  it('renders a server separation_of_duties refusal', async () => {
    setup(approval(), {
      [`POST ${A}/ap-1/decide`]: () => problemResponse(403, 'separation_of_duties'),
    });
    renderWithClient(<ApprovalsView />);
    const user = await open();
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('separation of duties');
  });

  it('indeterminate side effects warn prominently and need a comment and a confirm', async () => {
    const a = approval({ reason: 'indeterminate_side_effect' });
    const fetchMock = setup(a, {
      // The run state decides whether Retry is offered (waiting, within its deadline).
      'GET /api/v1/orgs/acme/runs/run-1': () =>
        jsonResponse(run({ id: 'run-1', status: 'waiting_for_approval' })),
      [`POST ${A}/ap-1/decide`]: () => jsonResponse({ ...a, status: 'approved' }),
    });
    renderWithClient(<ApprovalsView />);
    const user = await open();
    expect(screen.getByRole('alert')).toHaveTextContent('Indeterminate side effect');
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    const retry = await screen.findByRole('button', { name: 'Retry' });
    expect(retry).toBeDisabled(); // comment required
    expect(screen.getByRole('button', { name: 'Mark succeeded' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Fail' })).toBeDisabled();

    await user.type(screen.getByLabelText('Comment (required)'), 'checked the CRM');
    await user.click(screen.getByRole('button', { name: 'Mark succeeded' }));
    // Nothing is sent until confirmed.
    expect(fetchMock.calls.some((c) => c.key === `POST ${A}/ap-1/decide`)).toBe(false);
    const dlg = await screen.findByRole('dialog');
    await user.click(within(dlg).getByRole('button', { name: /Confirm Mark succeeded/ }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, `POST ${A}/ap-1/decide`)).toEqual({
        decision: 'mark_succeeded',
        comment: 'checked the CRM',
      }),
    );
  });

  it('cancelling the confirm sends nothing', async () => {
    const a = approval({ reason: 'indeterminate_side_effect' });
    const fetchMock = setup(a);
    renderWithClient(<ApprovalsView />);
    const user = await open();
    await user.type(screen.getByLabelText('Comment (required)'), 'unsure');
    await user.click(screen.getByRole('button', { name: 'Fail' }));
    const dlg = await screen.findByRole('dialog');
    await user.click(within(dlg).getByRole('button', { name: 'Cancel' }));
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('requests the selected status tab', async () => {
    const fetchMock = setup();
    renderWithClient(<ApprovalsView />);
    const user = userEvent.setup();
    await screen.findByRole('button', { name: /Review approval/ });
    await user.click(screen.getByRole('tab', { name: 'Expired' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.url.includes('status=expired'))).toBe(true),
    );
  });
});
