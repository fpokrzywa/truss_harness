import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { D, DELIVERY_PERMS, list, PLAN, stubDelivery, TARGET } from '@/test/delivery';
import { bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { PlansView } from './PlansView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const UNRESOLVED = 'The target has an unresolved unknown outcome.';
const TARGETS = {
  [`GET ${D}/targets`]: () =>
    list([TARGET, { ...TARGET, id: 't-2', name: 'Old target', status: 'archived' }]),
};

async function fillRequest(applicationKey = 'sales-app') {
  await userEvent.click(await screen.findByRole('button', { name: 'Request plan' }));
  const dialog = screen.getByRole('dialog');
  await waitFor(() =>
    expect(within(dialog).getByRole('option', { name: 'Analytics prod' })).toBeInTheDocument(),
  );
  expect(within(dialog).queryByRole('option', { name: 'Old target' })).not.toBeInTheDocument();
  await userEvent.selectOptions(within(dialog).getByLabelText('Target'), 't-1');
  await userEvent.type(within(dialog).getByLabelText('Application key'), applicationKey);
  await userEvent.type(within(dialog).getByLabelText('Display name'), 'Sales app');
  return dialog;
}

describe('PlansView list', () => {
  it('renders the plans with the target name and a link to each plan', async () => {
    stubDelivery();
    renderWithClient(<PlansView />);
    const table = await screen.findByRole('table', { name: 'Deployment plans' });
    const link = within(table).getByRole('link', { name: 'Sales app' });
    expect(link).toHaveAttribute('href', '/o/acme/deliver/plans/p-1');
    const row = link.closest('tr')!;
    expect(within(row).getByText('sales-app')).toBeInTheDocument();
    expect(await within(row).findByText('Analytics prod')).toBeInTheDocument();
    expect(within(row).getByText('sealed')).toBeInTheDocument();
  });

  it('falls back to the target id when the target is not listed', async () => {
    stubDelivery({ [`GET ${D}/targets`]: () => list([]) });
    renderWithClient(<PlansView />);
    expect(await screen.findByText('t-1')).toBeInTheDocument();
  });

  it('shows an empty state', async () => {
    stubDelivery({ [`GET ${D}/plans`]: () => list([]) });
    renderWithClient(<PlansView />);
    expect(await screen.findByText(/No deployment plans yet/)).toBeInTheDocument();
  });

  it('has no Request plan button without delivery:plan:request', async () => {
    stubDelivery(
      {},
      DELIVERY_PERMS.filter((p) => p !== 'delivery:plan:request'),
    );
    renderWithClient(<PlansView />);
    expect(await screen.findByRole('link', { name: 'Sales app' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Request plan' })).not.toBeInTheDocument();
  });
});

describe('PlansView request', () => {
  it('posts the request body for an active target and refreshes the list', async () => {
    const fetchMock = stubDelivery({
      ...TARGETS,
      [`POST ${D}/plans`]: () => jsonResponse({ ...PLAN, status: 'requested' }, { status: 201 }),
    });
    renderWithClient(<PlansView />);
    const dialog = await fillRequest();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/plans`)).toEqual({
      target_id: 't-1',
      application_key: 'sales-app',
      display_name: 'Sales app',
    });
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `GET ${D}/plans`).length).toBe(2),
    );
  });

  it('validates the application key before calling the API', async () => {
    const fetchMock = stubDelivery(TARGETS);
    renderWithClient(<PlansView />);
    const dialog = await fillRequest('Sales_App');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    expect(await within(dialog).findByText(/lowercase letters/)).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('shows a 409 target_unresolved problem inside the dialog', async () => {
    stubDelivery({
      ...TARGETS,
      [`POST ${D}/plans`]: () => problemResponse(409, 'target_unresolved', { detail: UNRESOLVED }),
    });
    renderWithClient(<PlansView />);
    const dialog = await fillRequest();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    const alert = await within(dialog).findByText(UNRESOLVED);
    expect(alert.closest('[role="alert"]')).toHaveTextContent('Could not request the plan');
    expect(within(dialog).getByText('target_unresolved')).toBeInTheDocument();
  });
});
