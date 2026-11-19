import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DeploymentDetail } from '@/lib/api/delivery-types';
import { D, DELIVERY_PERMS, DEPLOYMENT, DEPLOYMENT_DETAIL, stubDelivery } from '@/test/delivery';
import { bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { DeploymentDetailView } from './DeploymentDetailView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const GET_DETAIL = `GET ${D}/deployments/dep-1`;
const detail = (over: Partial<DeploymentDetail>) => ({
  [GET_DETAIL]: () => jsonResponse({ ...DEPLOYMENT_DETAIL, ...over }),
});
const UNKNOWN = detail({
  status: 'unknown_outcome',
  status_reason: 'apply lease lost',
  receipt: null,
  verification: null,
});
const NO_RESOLVE = DELIVERY_PERMS.filter((p) => p !== 'delivery:deployment:resolve');

const loaded = () => screen.findByRole('region', { name: 'Deployment' });

describe('DeploymentDetailView unknown outcome', () => {
  it('shows an alert with Reconcile and Mark as failed, never a healthy or cancelled status', async () => {
    stubDelivery(UNKNOWN);
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('may or may not have taken effect');
    expect(await within(alert).findByRole('button', { name: 'Reconcile' })).toBeInTheDocument();
    expect(within(alert).getByRole('button', { name: 'Mark as failed' })).toBeInTheDocument();
    expect(within(await loaded()).getByText('Outcome unknown')).toHaveClass('badge');
    for (const word of ['verified', 'healthy', 'cancelled']) {
      expect(screen.queryByText(word)).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: /Cancel deployment/ })).not.toBeInTheDocument();
  });

  it('reconciles and refreshes the deployment', async () => {
    const fetchMock = stubDelivery({
      ...UNKNOWN,
      [`POST ${D}/deployments/dep-1/reconcile`]: () =>
        jsonResponse({ ...DEPLOYMENT, status: 'unknown_outcome' }),
    });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Reconcile' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${D}/deployments/dep-1/reconcile`)).toBe(
        true,
      ),
    );
    await waitFor(() => expect(fetchMock.calls.filter((c) => c.key === GET_DETAIL).length).toBe(2));
  });

  it('shows a reconcile error instead of swallowing it', async () => {
    stubDelivery({
      ...UNKNOWN,
      [`POST ${D}/deployments/dep-1/reconcile`]: () =>
        problemResponse(409, 'conflict', { detail: 'no executor is online' }),
    });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Reconcile' }));
    expect(await screen.findByText('no executor is online')).toBeInTheDocument();
    expect(screen.getByText('Could not reconcile the deployment')).toBeInTheDocument();
  });

  it('marks failed only with a note, posting the trimmed note', async () => {
    const fetchMock = stubDelivery({
      ...UNKNOWN,
      [`POST ${D}/deployments/dep-1/mark-failed`]: () =>
        jsonResponse({ ...DEPLOYMENT, status: 'resolved_failed' }),
    });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Mark as failed' }));
    const dialog = screen.getByRole('dialog');
    const submit = within(dialog).getByRole('button', { name: 'Mark as failed' });
    expect(submit).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/Note/), '   ');
    expect(submit).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/Note/), 'App absent on the platform ');
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/deployments/dep-1/mark-failed`)).toEqual({
      note: 'App absent on the platform',
    });
  });

  it('has no Reconcile, Mark as failed or Cancel without delivery:deployment:resolve', async () => {
    stubDelivery(UNKNOWN, NO_RESOLVE);
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('may or may not have taken effect');
    await loaded();
    expect(screen.queryByRole('button', { name: 'Reconcile' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Mark as failed' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Cancel/ })).not.toBeInTheDocument();
  });
});

describe('DeploymentDetailView cancel', () => {
  it('cancels a queued deployment only after confirmation', async () => {
    const fetchMock = stubDelivery({
      ...detail({ status: 'queued', receipt: null, verification: null, journal: [] }),
      [`POST ${D}/deployments/dep-1/cancel`]: () =>
        jsonResponse({ ...DEPLOYMENT, status: 'cancelled' }),
    });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel deployment' }));
    expect(fetchMock.calls.some((c) => c.key.endsWith('/cancel'))).toBe(false);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAccessibleName('Cancel deployment?');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel deployment' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${D}/deployments/dep-1/cancel`)).toBe(
        true,
      ),
    );
  });

  it('has no Cancel button on a queued deployment without delivery:deployment:resolve', async () => {
    stubDelivery(detail({ status: 'queued' }), NO_RESOLVE);
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    await loaded();
    expect(screen.queryByRole('button', { name: /Cancel/ })).not.toBeInTheDocument();
  });

  it.each(['verified', 'applying', 'unknown_outcome'] as const)(
    'offers no Cancel for %s',
    async (status) => {
      stubDelivery(detail({ status }));
      renderWithClient(<DeploymentDetailView id="dep-1" />);
      await loaded();
      expect(screen.queryByRole('button', { name: /Cancel/ })).not.toBeInTheDocument();
    },
  );
});

describe('DeploymentDetailView fake platform', () => {
  it('shows the badge when the apply receipt handles say platform fake', async () => {
    stubDelivery(detail({ receipt: { operation_handles: { platform: 'fake' } } }));
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    expect(await screen.findByText('Fake platform')).toBeInTheDocument();
  });

  it('shows the badge when a journal receipt says platform fake', async () => {
    stubDelivery(
      detail({
        receipt: null,
        journal: [{ ...DEPLOYMENT_DETAIL.journal[1]!, receipt: { platform: 'fake' } }],
      }),
    );
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    expect(await screen.findByText('Fake platform')).toBeInTheDocument();
  });

  it('has no badge for a receipt without the marker', async () => {
    stubDelivery();
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    await loaded();
    expect(screen.queryByText('Fake platform')).not.toBeInTheDocument();
  });
});

describe('DeploymentDetailView content', () => {
  it('renders the status, journal, receipt, verification checks and experimental maturity', async () => {
    stubDelivery();
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    const summary = await loaded();
    expect(within(summary).getByText('verified')).toHaveClass('badge');
    expect(within(summary).getByText('Experimental')).toBeInTheDocument();
    expect(within(summary).getByRole('link', { name: 'p-1' })).toHaveAttribute(
      'href',
      '/o/acme/deliver/plans/p-1',
    );
    const journal = screen.getByRole('region', { name: 'Journal' });
    const steps = within(journal).getAllByRole('listitem');
    expect(steps).toHaveLength(2);
    expect(steps[0]).toHaveTextContent('app.create');
    expect(steps[0]).toHaveTextContent('intent');
    expect(steps[1]).toHaveTextContent('complete');
    expect(steps[1]).toHaveTextContent('d'.repeat(64));
    expect(screen.getByLabelText('Receipt JSON')).toHaveTextContent('"deployed_at"');
    const checks = screen.getByRole('table', { name: 'Verification checks' });
    const row = within(checks).getByText('app_running').closest('tr')!;
    expect(within(row).getByText('passed')).toBeInTheDocument();
    expect(within(row).getByText('Yes')).toBeInTheDocument();
    expect(within(row).getByText('state=RUNNING')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows placeholders, reconcile evidence and the resolution of a resolved failure', async () => {
    stubDelivery(
      detail({
        status: 'resolved_failed',
        receipt: null,
        verification: null,
        reconcile_evidence: { observed: 'absent' },
        resolved_at: '2026-11-18T11:00:00Z',
        resolved_note: 'App absent on the platform',
      }),
    );
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    expect(await screen.findByText('No receipt yet')).toBeInTheDocument();
    expect(screen.getByText('Not verified yet')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Reconcile evidence' })).toHaveTextContent(
      '"observed": "absent"',
    );
    expect(screen.getByText('App absent on the platform')).toBeInTheDocument();
  });

  it('renders markup in server values as literal text', async () => {
    const evil = '<img src=x onerror=alert(1)>';
    stubDelivery(detail({ status_reason: evil, receipt: { note: evil } }));
    const { container } = renderWithClient(<DeploymentDetailView id="dep-1" />);
    expect(await screen.findByText(evil)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });
});
