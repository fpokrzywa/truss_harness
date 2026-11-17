import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { approval, OPERATE_PERMS, run, stubOperate } from '@/test/operate';
import { list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { ApprovalsView } from './ApprovalsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const A = '/api/v1/orgs/acme/approvals';
const R = '/api/v1/orgs/acme/runs/run-1';

async function openIndeterminate(runOver = {}, runFails = false) {
  const a = approval({
    reason: 'indeterminate_side_effect',
    tool: null,
    expires_at: '2020-01-01T00:00:00Z', // already past: must not matter
    run_initiated_by: undefined,
  });
  stubOperate({
    [`GET ${A}`]: () => list([a]),
    [`GET ${A}/${a.id}`]: () => jsonResponse(a),
    [`GET ${R}`]: () =>
      runFails
        ? new Response('{}', { status: 500 })
        : jsonResponse(run({ id: 'run-1', deadline_at: '2099-01-01T00:00:00Z', ...runOver })),
  });
  renderWithClient(<ApprovalsView />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: /Review approval ap-1/ }));
  const detail = await screen.findByRole('region', { name: 'Approval detail' });
  return { user, detail };
}

describe('ApprovalsView: unknown write outcome (R1)', () => {
  it('shows no expiry wording or countdown and still allows deciding after expires_at', async () => {
    const { detail } = await openIndeterminate({ status: 'waiting_for_approval' });
    expect(within(detail).queryByRole('timer')).not.toBeInTheDocument();
    expect(detail).not.toHaveTextContent(/expire/i);
    expect(detail).not.toHaveTextContent(/left\)/);
    expect(await within(detail).findByRole('button', { name: 'Fail' })).toBeInTheDocument();
    expect(within(detail).getByLabelText('Comment (required)')).toBeInTheDocument();
  });

  it('hides retry when the run has ended', async () => {
    const { detail } = await openIndeterminate({ status: 'timed_out', outcome_unknown: true });
    expect(await within(detail).findByRole('button', { name: 'Mark succeeded' })).toBeVisible();
    expect(within(detail).getByRole('button', { name: 'Fail' })).toBeVisible();
    expect(within(detail).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('offers retry for a waiting run within its deadline', async () => {
    const { detail } = await openIndeterminate({ status: 'waiting_for_approval' });
    expect(await within(detail).findByRole('button', { name: 'Retry' })).toBeVisible();
  });

  it('withholds retry when the run state cannot be loaded', async () => {
    const { detail } = await openIndeterminate({}, true);
    expect(await within(detail).findByRole('button', { name: 'Fail' })).toBeVisible();
    expect(within(detail).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('keeps the expiry countdown for other approvals', async () => {
    const a = approval();
    stubOperate({
      [`GET ${A}`]: () => list([a]),
      [`GET ${A}/${a.id}`]: () => jsonResponse(a),
    });
    renderWithClient(<ApprovalsView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Review approval ap-1/ }));
    const detail = await screen.findByRole('region', { name: 'Approval detail' });
    expect(within(detail).getByRole('timer')).toHaveTextContent(/left/);
  });
});

describe('ApprovalsView: scoped approvers (R1)', () => {
  it('lists and offers decisions when the API allows it although the tenant-level check fails', async () => {
    const a = approval({
      reason: 'indeterminate_side_effect',
      tool: null,
      run_initiated_by: undefined,
    });
    stubOperate(
      {
        [`GET ${A}`]: () => list([a]),
        [`GET ${A}/${a.id}`]: () => jsonResponse(a),
        [`GET ${R}`]: () => jsonResponse(run({ id: 'run-1', status: 'timed_out' })),
      },
      OPERATE_PERMS.filter((p) => p !== 'approval:read' && p !== 'approval:decide'),
    );
    renderWithClient(<ApprovalsView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Review approval ap-1/ }));
    const detail = await screen.findByRole('region', { name: 'Approval detail' });
    expect(await within(detail).findByRole('button', { name: 'Fail' })).toBeVisible();
  });

  it('shows the no-access page only when the server answers 403', async () => {
    stubOperate({ [`GET ${A}`]: () => problemResponse(403, 'forbidden') });
    renderWithClient(<ApprovalsView />);
    expect(await screen.findByText(/no access|not found/i)).toBeInTheDocument();
  });
});
