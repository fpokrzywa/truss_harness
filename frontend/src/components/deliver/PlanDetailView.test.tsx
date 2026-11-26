import { screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { D, list, PLAN, PLAN_DIGEST, stubDelivery, TARGETS_OF_EACH_KIND } from '@/test/delivery';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { PlanDetailView } from './PlanDetailView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

describe('PlanDetailView', () => {
  it('shows a sealed plan with its full digest, steps and a live countdown', async () => {
    stubDelivery();
    renderWithClient(<PlanDetailView id="p-1" />);
    expect(await screen.findByText(PLAN_DIGEST)).toBeInTheDocument();
    expect(screen.getByRole('timer')).toHaveTextContent(/left\)$/);
    const steps = screen.getByRole('table', { name: 'Plan steps' });
    const row = within(steps).getByText('app.create').closest('tr')!;
    expect(within(row).getByText('create')).toBeInTheDocument();
    expect(within(row).getByText('apps/sales-app')).toBeInTheDocument();
    expect(within(row).getByText('Yes')).toBeInTheDocument();
    expect(screen.getByText('sales-app')).toBeInTheDocument();
    expect(screen.getByText('databricks-native/0.1.0')).toBeInTheDocument();
    expect(await screen.findByText('Analytics prod')).toBeInTheDocument();
  });

  it('shows Expired for a sealed plan whose expiry has passed', async () => {
    stubDelivery({
      [`GET ${D}/plans/p-1`]: () => jsonResponse({ ...PLAN, expires_at: '2020-01-01T00:00:00Z' }),
    });
    renderWithClient(<PlanDetailView id="p-1" />);
    expect(await screen.findByRole('timer')).toHaveTextContent('(Expired)');
  });

  it('shows Not sealed yet and an empty steps state before planning finishes', async () => {
    stubDelivery({
      [`GET ${D}/plans/p-1`]: () =>
        jsonResponse({ ...PLAN, status: 'planning', digest: null, steps: null }),
    });
    renderWithClient(<PlanDetailView id="p-1" />);
    expect(await screen.findByText('Not sealed yet')).toBeInTheDocument();
    expect(screen.getByText('No steps planned yet.')).toBeInTheDocument();
    expect(screen.queryByRole('timer')).not.toBeInTheDocument();
  });

  it.each([
    ['plan_failed', 'plan job failed: snowflake_patch_limit', 1],
    ['plan_failed', 'plan job failed: execution_failed', 0],
    ['plan_failed', 'snowflake_patch_limit', 0],
    ['rejected', 'plan job failed: snowflake_patch_limit', 0],
  ])(
    'shows the raw reason of a %s plan (%s) with %i patch limit hints',
    async (status, status_reason, hints) => {
      stubDelivery({
        [`GET ${D}/plans/p-1`]: () => jsonResponse({ ...PLAN, status, status_reason }),
      });
      renderWithClient(<PlanDetailView id="p-1" />);
      expect(await screen.findByText(status_reason)).toBeInTheDocument();
      expect(
        screen.queryAllByText(
          'This package has no free patch. Register a target with a new package and request the plan again.',
        ),
      ).toHaveLength(hints);
      expect(screen.queryAllByText(/no free patch/)).toHaveLength(hints);
    },
  );

  it.each([
    [
      'docker_host_mismatch',
      "The Docker daemon the executor reached is not this target's daemon. Run the executor on the target's host, or register a target with that host's daemon ID, then request the plan again.",
    ],
    [
      'foreign_project',
      "A compose project with this target's project name exists on the host and does not belong to this target. Register a target with another project name, or remove that project by hand, then request the plan again.",
    ],
    [
      'foreign_item',
      'The Fabric workspace already has an item with a name this target uses, and SETH did not create it. Register a target with other lakehouse and notebook names, or remove or rename that item, then request the plan again.',
    ],
    ['plan_stale', 'The target changed after this plan was made. Request a new plan.'],
    [
      'credential_unavailable',
      "The executor has no usable credential for this target. Check the target's credential reference in the executor's credentials file, then request the plan again.",
    ],
    [
      'snowflake_patch_limit',
      'This package has no free patch. Register a target with a new package and request the plan again.',
    ],
  ])('shows the hint of a plan that failed with %s under the raw reason', async (code, hint) => {
    const status_reason = `plan job failed: ${code}`;
    stubDelivery({
      [`GET ${D}/plans/p-1`]: () => jsonResponse({ ...PLAN, status: 'plan_failed', status_reason }),
    });
    renderWithClient(<PlanDetailView id="p-1" />);
    const reason = await screen.findByText(status_reason);
    expect(reason.closest('dd')).toHaveTextContent(`${status_reason}${hint}`);
  });

  it.each([
    ['plan_failed', 'plan job failed: execution_failed'],
    ['plan_failed', 'plan job failed: constructor'],
    ['plan_failed', 'plan job failed: __proto__'],
    ['plan_failed', 'docker_host_mismatch'],
    ['plan_failed', 'plan job failed: foreign_item and more'],
    ['plan_failed', 'plan job failed: plan_stale.'],
    ['rejected', 'plan job failed: foreign_project'],
    ['expired', 'plan job failed: plan_stale'],
  ])('shows no hint for a %s plan whose reason is %j', async (status, status_reason) => {
    stubDelivery({
      [`GET ${D}/plans/p-1`]: () => jsonResponse({ ...PLAN, status, status_reason }),
    });
    renderWithClient(<PlanDetailView id="p-1" />);
    const reason = await screen.findByText(status_reason);
    expect(reason.closest('dd')?.querySelector('p')).toBeNull();
  });

  const NOTE =
    "The installed product's content is not derived from the release. SETH records which release authorized the install; it does not check the product's content against it.";

  it.each(['sealed', 'approved', 'plan_failed', 'rejected'])(
    'shows the content note on a %s plan of a Palantir target',
    async (status) => {
      stubDelivery({
        [`GET ${D}/targets`]: () => list(TARGETS_OF_EACH_KIND),
        [`GET ${D}/plans/p-1`]: () => jsonResponse({ ...PLAN, status, target_id: 't-pal' }),
      });
      renderWithClient(<PlanDetailView id="p-1" />);
      expect(await screen.findByText(NOTE)).toBeInTheDocument();
      expect(screen.getByText('Foundry prod')).toBeInTheDocument();
    },
  );

  it.each(['t-1', 't-sf', 't-fab', 't-dock', 't-gone'])(
    'shows no content note on a plan of target %s',
    async (target_id) => {
      const fetchMock = stubDelivery({
        [`GET ${D}/targets`]: () => list(TARGETS_OF_EACH_KIND),
        [`GET ${D}/plans/p-1`]: () => jsonResponse({ ...PLAN, target_id }),
      });
      renderWithClient(<PlanDetailView id="p-1" />);
      await screen.findByText(PLAN_DIGEST);
      await waitFor(() =>
        expect(fetchMock.calls.some((c) => c.key === `GET ${D}/targets`)).toBe(true),
      );
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.queryByText(/content is not derived/)).not.toBeInTheDocument();
    },
  );

  it('shows the decision and links to the deployment', async () => {
    stubDelivery({
      [`GET ${D}/plans/p-1`]: () =>
        jsonResponse({
          ...PLAN,
          status: 'approved',
          approval: {
            decision: 'approve',
            decided_by: 'u-other',
            decided_at: '2026-11-18T10:01:00Z',
            note: 'Looks right',
          },
          deployment_id: 'dep-1',
        }),
    });
    renderWithClient(<PlanDetailView id="p-1" />);
    expect(await screen.findByRole('link', { name: 'dep-1' })).toHaveAttribute(
      'href',
      '/o/acme/deliver/deployments/dep-1',
    );
    expect(screen.getByText('Approved')).toBeInTheDocument();
    expect(screen.getByText('Looks right')).toBeInTheDocument();
  });
});
