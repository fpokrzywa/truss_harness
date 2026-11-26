import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DeploymentDetail } from '@/lib/api/delivery-types';
import {
  D,
  DELIVERY_PERMS,
  DEPLOYMENT,
  DEPLOYMENT_DETAIL,
  deploymentWithSteps,
  PLAN,
  SNOWFLAKE_DEPLOYMENT_DETAIL,
  SNOWFLAKE_STEPS,
  SNOWFLAKE_TARGET,
  stubDelivery,
  TARGET,
  TARGETS_OF_EACH_KIND,
  unusable,
} from '@/test/delivery';
import { ORG, RELEASE } from '@/test/releases';
import { bodyOf, list } from '@/test/security';
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

  it('offers only Mark as failed, with the reason, on a target whose settings are unusable', async () => {
    const fetchMock = stubDelivery({
      ...UNKNOWN,
      [`GET ${D}/targets`]: () => list([unusable(TARGET)]),
      [`POST ${D}/deployments/dep-1/mark-failed`]: () =>
        jsonResponse({ ...DEPLOYMENT, status: 'resolved_failed' }),
    });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    const alert = await screen.findByRole('alert');
    expect(
      await within(alert).findByText(
        "This target's stored settings are no longer valid, so this deployment cannot be reconciled. Check the platform, then mark the deployment as failed.",
      ),
    ).toBeInTheDocument();
    expect(within(alert).queryByRole('button', { name: 'Reconcile' })).not.toBeInTheDocument();
    await userEvent.click(within(alert).getByRole('button', { name: 'Mark as failed' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText(/Note/), 'App absent');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Mark as failed' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, `POST ${D}/deployments/dep-1/mark-failed`)).toEqual({
        note: 'App absent',
      }),
    );
  });

  it('keeps Reconcile and says nothing of unusable settings on a usable target', async () => {
    stubDelivery({ ...UNKNOWN, [`GET ${D}/targets`]: () => list([TARGET]) });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    const alert = await screen.findByRole('alert');
    expect(await within(alert).findByRole('button', { name: 'Reconcile' })).toBeInTheDocument();
    expect(within(await loaded()).getByText(/^Databricks/)).toBeInTheDocument();
    expect(screen.queryByText(/no longer valid/)).not.toBeInTheDocument();
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
    expect(await within(summary).findByText('Experimental')).toBeInTheDocument();
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

  it('shows a not_run check as a neutral Not run, and the other states as before', async () => {
    stubDelivery(
      detail({
        verification: {
          checks: [
            { name: 'app_running', status: 'passed', required: true, evidence: 'ok' },
            { name: 'content-check', status: 'not_run', required: false, evidence: 'none' },
            { name: 'frontend-asset', status: 'failed', required: true, evidence: 'differs' },
            { name: 'extra', status: 'skipped', required: false, evidence: 'n/a' },
            { name: 'later', status: 'pending_review', required: false, evidence: '' },
          ],
        },
      }),
    );
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    expect(within(await loaded()).getByText('verified')).toHaveClass('badge');
    const checks = screen.getByRole('table', { name: 'Verification checks' });
    const cells = (name: string) =>
      within(within(checks).getByText(name).closest('tr')!).getAllByRole('cell');
    const notRun = cells('content-check');
    expect(within(notRun[1]!).getByText('Not run')).toHaveClass('badge');
    expect(within(notRun[1]!).getByText('Not run').className).toBe(
      within(cells('extra')[1]!).getByText('skipped').className,
    );
    expect(notRun[2]).toHaveTextContent(/^No$/);
    expect(within(checks).queryByText('not_run')).not.toBeInTheDocument();
    expect(within(cells('app_running')[1]!).getByText('passed').className).toMatch(/green/);
    expect(within(cells('frontend-asset')[1]!).getByText('failed').className).toMatch(/red/);
    expect(within(cells('extra')[1]!).getByText('skipped').className).toMatch(/slate/);
    expect(within(cells('later')[1]!).getByText('pending_review').className).toMatch(/slate/);
  });

  it('shows the plan_stale hint under an apply refused for a stale plan', async () => {
    stubDelivery(detail({ status: 'failed', status_reason: 'apply refused: plan_stale' }));
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    const reason = await screen.findByText('apply refused: plan_stale');
    expect(reason.closest('dd')).toHaveTextContent(
      'apply refused: plan_staleThe target changed after this plan was made. Request a new plan.',
    );
  });

  it.each(['apply refused: plan_stale (retry)', 'plan job failed: plan_stale'])(
    'shows no hint for the reason %j',
    async (status_reason) => {
      stubDelivery(detail({ status: 'failed', status_reason }));
      renderWithClient(<DeploymentDetailView id="dep-1" />);
      expect(await screen.findByText(status_reason)).toBeInTheDocument();
      expect(screen.queryByText(/Request a new plan/)).not.toBeInTheDocument();
    },
  );

  it('renders markup in server values as literal text', async () => {
    const evil = '<img src=x onerror=alert(1)>';
    stubDelivery(detail({ status_reason: evil, receipt: { note: evil } }));
    const { container } = renderWithClient(<DeploymentDetailView id="dep-1" />);
    expect(await screen.findByText(evil)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });
});

describe('DeploymentDetailView platform', () => {
  const targets = { [`GET ${D}/targets`]: () => list([TARGET, SNOWFLAKE_TARGET]) };
  const kind = async () => {
    const summary = await loaded();
    return within(summary).getByText('Kind').nextElementSibling as HTMLElement;
  };

  it.each([
    ['Databricks', 't-1'],
    ['Snowflake', 't-sf'],
  ])('shows %s and Experimental for a deployment on that platform', async (label, target_id) => {
    stubDelivery({ ...targets, ...detail({ target_id }) });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    const row = await kind();
    await waitFor(() => expect(row).toHaveTextContent(new RegExp(`^${label} Experimental$`)));
  });

  it('shows - and no platform name for a target that is not in the list', async () => {
    const fetchMock = stubDelivery({ ...targets, ...detail({ target_id: 't-gone' }) });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    const row = await kind();
    // once the list of targets has been answered
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `GET ${D}/targets`)).toBe(true),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(row).toHaveTextContent(/^-$/);
    for (const word of ['Databricks', 'Snowflake', 'Experimental']) {
      expect(screen.queryByText(new RegExp(word))).not.toBeInTheDocument();
    }
  });

  it('lists the eight Snowflake steps of the journal in server order', async () => {
    stubDelivery({ ...targets, [GET_DETAIL]: () => jsonResponse(SNOWFLAKE_DEPLOYMENT_DETAIL) });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    const journal = await screen.findByRole('region', { name: 'Journal' });
    const entries = within(journal)
      .getAllByRole('listitem')
      .map((li) => Array.from(li.querySelectorAll('span'), (s) => s.textContent).slice(0, 2));
    expect(entries).toEqual(
      SNOWFLAKE_STEPS.flatMap((step) => [
        [step, 'intent'],
        [step, 'complete'],
      ]),
    );
    expect(SNOWFLAKE_STEPS).toHaveLength(8);
  });

  it.each([
    ['Microsoft Fabric', 't-fab', ['lakehouse', 'notebook', 'fabric-finalize']],
    ['Palantir Foundry', 't-pal', ['installation', 'palantir-finalize']],
    ['Docker', 't-dock', ['image-build', 'image-verify', 'compose-up', 'container-ready']],
  ])('shows %s, Experimental and the journal in server order', async (label, target_id, steps) => {
    stubDelivery({
      [`GET ${D}/targets`]: () => list(TARGETS_OF_EACH_KIND),
      [GET_DETAIL]: () => jsonResponse(deploymentWithSteps(target_id, steps)),
    });
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    const row = await kind();
    await waitFor(() => expect(row).toHaveTextContent(new RegExp(`^${label} Experimental$`)));
    const journal = screen.getByRole('region', { name: 'Journal' });
    const entries = within(journal)
      .getAllByRole('listitem')
      .map((li) => Array.from(li.querySelectorAll('span'), (s) => s.textContent).slice(0, 2));
    expect(entries).toEqual(
      steps.flatMap((step) => [
        [step, 'intent'],
        [step, 'complete'],
      ]),
    );
  });
});

describe('DeploymentDetailView Ask SETH', () => {
  const WRITE = [...DELIVERY_PERMS, 'assist:request:write'];
  const GET_PLAN = `GET ${D}/plans/p-1`;
  const GET_RELEASE = `GET ${ORG}/releases/${PLAN.release_id}`;
  const workspaceTarget = {
    [`GET ${D}/targets`]: () => list([{ ...TARGET, project: null }]),
  };
  const ask = () => screen.findByRole('link', { name: 'Ask SETH' });
  const called = (fetchMock: ReturnType<typeof stubDelivery>, key: string) =>
    fetchMock.calls.some((c) => c.key === key);
  /** Once everything the screen asked for has been answered. */
  const noLink = async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('link', { name: 'Ask SETH' })).not.toBeInTheDocument();
  };

  it('uses the project of the target without reading the plan, beside the existing actions', async () => {
    const fetchMock = stubDelivery(detail({ status: 'queued' }), WRITE);
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    expect(await ask()).toHaveAttribute(
      'href',
      '/o/acme/w/eng/p/api/assist?context=deployment:dep-1',
    );
    expect(await screen.findByRole('button', { name: 'Cancel deployment' })).toBeEnabled();
    expect(called(fetchMock, GET_PLAN)).toBe(false);
  });

  it('is absent without assist:request:write', async () => {
    stubDelivery();
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    await within(await loaded()).findByText('Analytics prod');
    await noLink();
  });

  it('uses the project of the released plan on a workspace-level target', async () => {
    stubDelivery(
      {
        ...workspaceTarget,
        [GET_RELEASE]: () => jsonResponse({ ...RELEASE, project: 'web' }),
      },
      WRITE,
    );
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    expect(await ask()).toHaveAttribute(
      'href',
      '/o/acme/w/eng/p/web/assist?context=deployment:dep-1',
    );
  });

  it('is absent when the plan has no release', async () => {
    const fetchMock = stubDelivery(
      { ...workspaceTarget, [GET_PLAN]: () => jsonResponse({ ...PLAN, release_id: null }) },
      WRITE,
    );
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    await waitFor(() => expect(called(fetchMock, GET_PLAN)).toBe(true));
    await noLink();
    expect(called(fetchMock, GET_RELEASE)).toBe(false);
  });

  it('is absent when the release is in another workspace', async () => {
    const fetchMock = stubDelivery(
      {
        ...workspaceTarget,
        [GET_RELEASE]: () => jsonResponse({ ...RELEASE, workspace: 'other' }),
      },
      WRITE,
    );
    renderWithClient(<DeploymentDetailView id="dep-1" />);
    await waitFor(() => expect(called(fetchMock, GET_RELEASE)).toBe(true));
    await noLink();
  });
});
