import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SESSION } from '@/test/security';
import { GOV_PERMS, stubGov } from '@/test/governance';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { PlatformRateCardsView, PricingView } from './PricingView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const RC = '/api/v1/orgs/acme/rate-cards';
const card = { id: 'rc1', provider_key: 'fake', model_key: 'fake-chat', name: 'Fake chat' };
const version = {
  id: 'rcv1',
  version_no: 1,
  effective_from: '2026-01-01T00:00:00Z',
  effective_to: null,
  currency: 'USD',
  unit: 'per_1m_tokens',
  input_rate: 1,
  output_rate: 2,
  source: 'SAMPLE — not vendor pricing',
  revision: '1',
};

async function openVersions(routes: Parameters<typeof stubGov>[0] = {}, perms = GOV_PERMS) {
  const fetchMock = stubGov(
    {
      [`GET ${RC}`]: () => list([card]),
      [`GET ${RC}/rc1/versions`]: () =>
        list([version, { ...version, id: 'rcv2', version_no: 2, source: 'Vendor list 2026-10' }]),
      ...routes,
    },
    perms,
  );
  renderWithClient(<PricingView />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Versions of Fake chat' }));
  return { fetchMock, user };
}

async function fillVersion(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement) {
  await user.type(within(dialog).getByLabelText('Effective from'), '2026-06-01');
  await user.type(within(dialog).getByLabelText('Input rate'), '3');
  await user.type(within(dialog).getByLabelText('Output rate'), '15');
  await user.type(within(dialog).getByLabelText('Cached read rate (optional)'), '0.3');
  await user.type(within(dialog).getByLabelText('Source'), 'Vendor list 2026-10');
}

describe('PricingView', () => {
  it('flags SAMPLE sources and shows versions as immutable', async () => {
    await openVersions();
    const table = await screen.findByRole('table', { name: 'Rate card versions' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(
      within(rows[0] as HTMLElement).getByText('SAMPLE: not vendor pricing'),
    ).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).queryByText(/SAMPLE/)).not.toBeInTheDocument();
    expect(screen.getByText(/There is no edit or delete/)).toBeInTheDocument();
    // No edit/delete controls on versions.
    expect(screen.queryByRole('button', { name: /edit|delete|remove/i })).not.toBeInTheDocument();
  });

  it('creates a version with the contract payload', async () => {
    const { fetchMock, user } = await openVersions({
      [`POST ${RC}/rc1/versions`]: () => jsonResponse({ ...version, id: 'rcv3' }, { status: 201 }),
    });
    await user.click(await screen.findByRole('button', { name: 'Add version' }));
    const dialog = screen.getByRole('dialog');
    await fillVersion(user, dialog);
    await user.click(within(dialog).getByRole('button', { name: 'Create version' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${RC}/rc1/versions`)).toBe(true),
    );
    expect(bodyOf(fetchMock, `POST ${RC}/rc1/versions`)).toEqual({
      effective_from: '2026-06-01T00:00:00.000Z',
      currency: 'USD',
      unit: 'per_1m_tokens',
      input_rate: 3,
      output_rate: 15,
      cached_read_rate: 0.3,
      source: 'Vendor list 2026-10',
      revision: '1',
    });
  });

  it('shows an overlapping effective range (409) and keeps the dialog open', async () => {
    const { user } = await openVersions({
      [`POST ${RC}/rc1/versions`]: () =>
        problemResponse(409, 'rate_card_overlap', { detail: 'Overlaps version 2.' }),
    });
    await user.click(await screen.findByRole('button', { name: 'Add version' }));
    const dialog = screen.getByRole('dialog');
    await fillVersion(user, dialog);
    await user.click(within(dialog).getByRole('button', { name: 'Create version' }));
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('overlaps an existing version');
    expect(alert).toHaveTextContent('Overlaps version 2.');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('explains a past start that would close the open-ended version (409)', async () => {
    const { user } = await openVersions({
      [`POST ${RC}/rc1/versions`]: () =>
        problemResponse(409, 'rate_card_overlap', {
          detail: 'A new version cannot start in the past.',
        }),
    });
    await user.click(await screen.findByRole('button', { name: 'Add version' }));
    const dialog = screen.getByRole('dialog');
    await fillVersion(user, dialog);
    await user.click(within(dialog).getByRole('button', { name: 'Create version' }));
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('choose a start date after today');
    expect(alert).not.toHaveTextContent('overlaps an existing version');
  });

  it('validates rates and the date range before enabling create', async () => {
    const { user } = await openVersions();
    await user.click(await screen.findByRole('button', { name: 'Add version' }));
    const dialog = screen.getByRole('dialog');
    const create = within(dialog).getByRole('button', { name: 'Create version' });
    expect(create).toBeDisabled();
    await fillVersion(user, dialog);
    expect(create).toBeEnabled();
    await user.type(within(dialog).getByLabelText('Effective to (optional)'), '2026-05-01');
    expect(within(dialog).getByText('The end must be after the start.')).toBeInTheDocument();
    expect(create).toBeDisabled();
  });

  it('hides creation without pricing:manage', async () => {
    await openVersions(
      {},
      GOV_PERMS.filter((p) => p !== 'pricing:manage'),
    );
    await screen.findByRole('table', { name: 'Rate card versions' });
    expect(screen.queryByRole('button', { name: 'Create rate card' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add version' })).not.toBeInTheDocument();
  });
});

describe('PlatformRateCardsView', () => {
  it('lists global cards for platform admins and posts versions under /platform', async () => {
    const fetchMock = stubGov({
      'GET /api/auth/session': () =>
        jsonResponse({ ...SESSION, user: { ...SESSION.user, is_platform_admin: true } }),
      'GET /api/v1/platform/rate-cards': () => list([card]),
      'GET /api/v1/platform/rate-cards/rc1/versions': () => list([]),
    });
    renderWithClient(<PlatformRateCardsView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Versions of Fake chat' }));
    await screen.findByText('No versions yet.');
    expect(
      fetchMock.calls.some((c) => c.key === 'GET /api/v1/platform/rate-cards/rc1/versions'),
    ).toBe(true);
  });

  it('refuses non-admins without calling the API', async () => {
    const fetchMock = stubGov({});
    renderWithClient(<PlatformRateCardsView />);
    expect(await screen.findByRole('alert')).toHaveTextContent('platform administrators only');
    expect(fetchMock.calls.some((c) => c.url.includes('/platform/rate-cards'))).toBe(false);
  });
});
