import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { D, DELIVERY_PERMS, list, PLAN, PLAN_DIGEST, stubDelivery } from '@/test/delivery';
import { bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { PlanApprovalsView } from './PlanApprovalsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const OWN = 'a different person must approve or reject it';

describe('PlanApprovalsView list', () => {
  it('lists sealed plans with target, digest, requester and countdown', async () => {
    const fetchMock = stubDelivery();
    renderWithClient(<PlanApprovalsView />);
    const table = await screen.findByRole('table', { name: 'Plans waiting for a decision' });
    const row = within(table).getByRole('link', { name: 'Sales app' }).closest('tr')!;
    expect(within(row).getByText(PLAN_DIGEST)).toBeInTheDocument();
    expect(within(row).getByText('u-other')).toBeInTheDocument();
    expect(within(row).getByRole('timer')).toHaveTextContent(/left$/);
    expect(await within(row).findByText('Analytics prod')).toBeInTheDocument();
    const call = fetchMock.calls.find((c) => c.key === `GET ${D}/plans`)!;
    expect(new URL(call.url, 'http://x').searchParams.get('status')).toBe('sealed');
  });

  it('shows an empty state', async () => {
    stubDelivery({ [`GET ${D}/plans`]: () => list([]) });
    renderWithClient(<PlanApprovalsView />);
    expect(await screen.findByText('No plans are waiting for a decision.')).toBeInTheDocument();
  });
});

describe('PlanApprovalsView decisions', () => {
  it('approves with the plan digest exactly as shown, then refreshes plans and deployments', async () => {
    const fetchMock = stubDelivery({
      [`POST ${D}/plans/p-1/approve`]: () => jsonResponse({ ...PLAN, status: 'approved' }),
    });
    renderWithClient(<PlanApprovalsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Approve/ }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(PLAN_DIGEST)).toBeInTheDocument();
    await userEvent.type(within(dialog).getByLabelText('Note (optional)'), 'Reviewed');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Approve plan' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/plans/p-1/approve`)).toEqual({
      plan_digest: PLAN.digest,
      note: 'Reviewed',
    });
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `GET ${D}/plans`).length).toBe(2),
    );
  });

  it('rejects after confirmation', async () => {
    const fetchMock = stubDelivery({
      [`POST ${D}/plans/p-1/reject`]: () => jsonResponse({ ...PLAN, status: 'rejected' }),
    });
    renderWithClient(<PlanApprovalsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Reject/ }));
    expect(fetchMock.calls.some((c) => c.key.endsWith('/reject'))).toBe(false);
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Reject plan' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${D}/plans/p-1/reject`)).toBe(true),
    );
    expect(bodyOf(fetchMock, `POST ${D}/plans/p-1/reject`)).toEqual({});
  });

  it('shows a server refusal instead of swallowing it', async () => {
    stubDelivery({
      [`POST ${D}/plans/p-1/approve`]: () =>
        problemResponse(409, 'self_approval_forbidden', { detail: 'the requester cannot decide' }),
    });
    renderWithClient(<PlanApprovalsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Approve/ }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Approve plan' }));
    expect(await within(dialog).findByText('the requester cannot decide')).toBeInTheDocument();
    expect(within(dialog).getByText('Could not approve the plan')).toBeInTheDocument();
  });

  it.each([
    [
      'workspace_not_claimed',
      "The Docker target's workspace is not a target of this organization.",
      "The Docker target's Databricks workspace is not registered as a Databricks target of this organization. Register that workspace as a target first, then try again.",
    ],
    [
      'target_bindings_invalid',
      "The target's stored bindings are invalid.",
      "This target's stored settings are no longer valid. Archive the target, register it again and request the plan on the new one.",
    ],
  ])(
    'shows an approval refused with %s with its detail, code and hint',
    async (code, detail, hint) => {
      stubDelivery({
        [`POST ${D}/plans/p-1/approve`]: () => problemResponse(409, code, { detail }),
      });
      renderWithClient(<PlanApprovalsView />);
      await userEvent.click(await screen.findByRole('button', { name: /Approve/ }));
      const dialog = screen.getByRole('dialog');
      await userEvent.click(within(dialog).getByRole('button', { name: 'Approve plan' }));
      const alert = (await within(dialog).findByText(detail)).closest('[role="alert"]');
      expect(alert).toHaveTextContent('Could not approve the plan');
      expect(within(dialog).getByText(code)).toBeInTheDocument();
      expect(within(dialog).getByText(hint)).toBeInTheDocument();
    },
  );

  it('disables both buttons with a visible reason on a plan the user requested', async () => {
    stubDelivery({ [`GET ${D}/plans`]: () => list([{ ...PLAN, requested_by: 'u-me' }]) });
    renderWithClient(<PlanApprovalsView />);
    expect(await screen.findByText(new RegExp(OWN))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Approve/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Reject/ })).toBeDisabled();
  });

  it('shows Expired and disables both buttons once the plan has expired', async () => {
    stubDelivery({
      [`GET ${D}/plans`]: () => list([{ ...PLAN, expires_at: '2020-01-01T00:00:00Z' }]),
    });
    renderWithClient(<PlanApprovalsView />);
    expect(await screen.findByRole('timer')).toHaveTextContent('Expired');
    expect(screen.getByRole('button', { name: /Approve/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Reject/ })).toBeDisabled();
  });

  it('has no Approve or Reject buttons without delivery:plan:approve', async () => {
    stubDelivery(
      {},
      DELIVERY_PERMS.filter((p) => p !== 'delivery:plan:approve'),
    );
    renderWithClient(<PlanApprovalsView />);
    expect(await screen.findByRole('link', { name: 'Sales app' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Reject/ })).not.toBeInTheDocument();
  });
});
